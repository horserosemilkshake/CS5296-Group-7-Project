import asyncio
import contextlib
import json
import logging
import os
import uuid
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LLM_API_MODE = os.getenv("LLM_API_MODE", "ollama").lower()
LLM_API_BASE = os.getenv("LLM_API_BASE", "http://ollama:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.2")
SUMMARIZE_MAX_CONCURRENCY = max(int(os.getenv("SUMMARIZE_MAX_CONCURRENCY", "2")), 1)
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
NLP_JOB_REQUEST_TOPIC = os.getenv("NLP_JOB_REQUEST_TOPIC", "nlp.jobs.request")
NLP_JOB_RESULT_TOPIC = os.getenv("NLP_JOB_RESULT_TOPIC", "nlp.jobs.result")
NLP_ASYNC_ENABLED = os.getenv("NLP_ASYNC_ENABLED", "false").lower() == "true"

app = FastAPI(title="Realtime ASR NLP Service")
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SummarizeRequest(BaseModel):
    text: str


class SummarizeResponse(BaseModel):
    summary: str


class RewriteRequest(BaseModel):
    text: str
    prompt: str


class RewriteResponse(BaseModel):
    rewritten_text: str


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: str | None = None
    error: str | None = None


def build_llm_error_detail(exc: Exception) -> str:
    """Create user-facing LLM error details with Ollama response context."""
    if isinstance(exc, HTTPException):
        return str(exc.detail)

    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        detail = ""

        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or payload)
            else:
                detail = str(payload)
        except Exception:
            detail = (response.text or "").strip()

        if len(detail) > 500:
            detail = detail[:500] + "..."

        detail_lc = detail.lower()
        if any(token in detail_lc for token in ["not enough", "insufficient", "out of memory", "memory"]):
            return (
                f"Ollama returned HTTP {status}: {detail}. "
                "Host likely lacks RAM for this model; use a smaller model (for example `llama3.2:1b`) "
                "or lower NLP concurrency."
            )

        if any(token in detail_lc for token in ["model", "not found", "unknown"]):
            return (
                f"Ollama returned HTTP {status}: {detail}. "
                "Model may be missing; verify `LLM_MODEL` and ensure it is pulled in Ollama."
            )

        return f"Ollama returned HTTP {status}: {detail or str(exc)}"

    if isinstance(exc, httpx.HTTPError):
        return f"Failed to reach LLM API: {exc}"

    return f"Unexpected NLP error: {exc}"


@app.on_event("startup")
async def startup_event() -> None:
    app.state.http_client = httpx.AsyncClient(timeout=120)
    app.state.nlp_limiter = asyncio.Semaphore(SUMMARIZE_MAX_CONCURRENCY)
    app.state.model_ready = False
    app.state.jobs = {}
    if NLP_ASYNC_ENABLED:
        app.state.kafka_producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        app.state.kafka_consumer = AIOKafkaConsumer(
            NLP_JOB_REQUEST_TOPIC,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id="nlp-workers",
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            auto_offset_reset="latest",
        )
        await app.state.kafka_producer.start()
        app.state.worker_task = asyncio.create_task(job_worker_loop())


async def ensure_ollama_model_ready(client: httpx.AsyncClient) -> None:
    # The first pull can take several minutes; keep this timeout generous.
    pull_client = httpx.AsyncClient(timeout=1800)
    try:
        tags_response = await client.get(f"{LLM_API_BASE.rstrip('/')}/api/tags")
        tags_response.raise_for_status()
        tags_payload: dict[str, Any] = tags_response.json()
        models = tags_payload.get("models", [])
        model_names = {m.get("name") for m in models if isinstance(m, dict)}
        if LLM_MODEL in model_names or f"{LLM_MODEL}:latest" in model_names:
            app.state.model_ready = True
            return

        logger.info("Model %s not found in Ollama. Pulling model...", LLM_MODEL)
        pull_response = await pull_client.post(
            f"{LLM_API_BASE.rstrip('/')}/api/pull",
            json={"model": LLM_MODEL, "stream": False},
        )
        pull_response.raise_for_status()
        app.state.model_ready = True
        logger.info("Model %s is ready.", LLM_MODEL)
    finally:
        await pull_client.aclose()


@app.on_event("startup")
async def warmup_model_event() -> None:
    if LLM_API_MODE != "ollama":
        app.state.model_ready = True
        return

    # Spawn a background task so startup completes immediately.
    # The readiness probe will return 503 until the model is confirmed ready,
    # but the pod won't crash-loop — it keeps retrying.
    app.state.model_warmup_task = asyncio.create_task(_warmup_with_retry())


async def _warmup_with_retry(retry_interval: float = 10.0) -> None:
    """Poll Ollama until the model is ready, retrying indefinitely."""
    attempt = 0
    while True:
        attempt += 1
        try:
            await ensure_ollama_model_ready(app.state.http_client)
            logger.info("NLP model warmup succeeded on attempt %d.", attempt)
            return
        except Exception:
            logger.warning(
                "NLP model warmup attempt %d failed; retrying in %.0fs.",
                attempt,
                retry_interval,
            )
            await asyncio.sleep(retry_interval)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    worker_task: asyncio.Task | None = getattr(app.state, "worker_task", None)
    if worker_task is not None:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    consumer: AIOKafkaConsumer | None = getattr(app.state, "kafka_consumer", None)
    if consumer is not None:
        await consumer.stop()

    producer: AIOKafkaProducer | None = getattr(app.state, "kafka_producer", None)
    if producer is not None:
        await producer.stop()

    warmup_task: asyncio.Task | None = getattr(app.state, "model_warmup_task", None)
    if warmup_task is not None:
        warmup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warmup_task

    await app.state.http_client.aclose()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model_ready": bool(getattr(app.state, "model_ready", False))}


@app.get("/ready")
async def ready() -> dict:
    model_ready = bool(getattr(app.state, "model_ready", False))
    if not model_ready:
        raise HTTPException(status_code=503, detail="NLP model is not ready yet")
    return {"status": "ready"}


async def summarize_with_ollama(client: httpx.AsyncClient, text: str) -> str:
    prompt = (
        "Summarize the following transcript into concise key points and one short paragraph.\n\n"
        f"Transcript:\n{text}"
    )

    response = await client.post(
        f"{LLM_API_BASE.rstrip('/')}/api/generate",
        json={
            "model": LLM_MODEL,
            "prompt": prompt,
            "stream": False,
        },
    )

    response.raise_for_status()
    data = response.json()
    summary = data.get("response", "").strip()
    if not summary:
        raise HTTPException(status_code=502, detail="LLM returned empty summary")
    return summary


async def rewrite_with_ollama(client: httpx.AsyncClient, text: str, instruction: str) -> str:
    prompt = (
        "Rewrite the transcript according to the instruction. "
        "Preserve factual meaning unless the instruction explicitly asks otherwise. "
        "Return only the rewritten transcript, with no preface.\n\n"
        f"Instruction:\n{instruction}\n\n"
        f"Transcript:\n{text}"
    )

    response = await client.post(
        f"{LLM_API_BASE.rstrip('/')}/api/generate",
        json={
            "model": LLM_MODEL,
            "prompt": prompt,
            "stream": False,
        },
    )

    response.raise_for_status()
    data = response.json()
    rewritten = data.get("response", "").strip()
    if not rewritten:
        raise HTTPException(status_code=502, detail="LLM returned empty rewrite")
    return rewritten


async def summarize_with_openai_compatible(client: httpx.AsyncClient, text: str) -> str:
    response = await client.post(
        f"{LLM_API_BASE.rstrip('/')}/v1/chat/completions",
        json={
            "model": LLM_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "You summarize transcripts clearly and briefly.",
                },
                {
                    "role": "user",
                    "content": (
                        "Summarize this transcript into concise key points and one short paragraph:\n\n"
                        f"{text}"
                    ),
                },
            ],
            "temperature": 0.2,
        },
    )

    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices", [])
    if not choices:
        raise HTTPException(status_code=502, detail="No completion choices returned")

    message = choices[0].get("message", {})
    summary = message.get("content", "").strip()
    if not summary:
        raise HTTPException(status_code=502, detail="LLM returned empty summary")

    return summary


async def rewrite_with_openai_compatible(client: httpx.AsyncClient, text: str, instruction: str) -> str:
    response = await client.post(
        f"{LLM_API_BASE.rstrip('/')}/v1/chat/completions",
        json={
            "model": LLM_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You rewrite transcripts. Follow the user instruction exactly and "
                        "return only rewritten transcript text."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Instruction:\n{instruction}\n\n"
                        f"Transcript:\n{text}"
                    ),
                },
            ],
            "temperature": 0.2,
        },
    )

    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices", [])
    if not choices:
        raise HTTPException(status_code=502, detail="No completion choices returned")

    message = choices[0].get("message", {})
    rewritten = message.get("content", "").strip()
    if not rewritten:
        raise HTTPException(status_code=502, detail="LLM returned empty rewrite")

    return rewritten


async def process_job(job: dict[str, Any]) -> tuple[str | None, str | None]:
    if not bool(getattr(app.state, "model_ready", False)):
        return None, "NLP model is not ready yet"

    kind = job.get("kind", "")
    text = str(job.get("text", "")).strip()
    prompt = str(job.get("prompt", "")).strip()

    if not text:
        return None, "Transcript text cannot be empty"

    try:
        async with app.state.nlp_limiter:
            if kind == "summarize":
                if LLM_API_MODE == "openai":
                    summary = await summarize_with_openai_compatible(app.state.http_client, text)
                else:
                    summary = await summarize_with_ollama(app.state.http_client, text)
                return summary, None

            if kind == "rewrite":
                if not prompt:
                    return None, "Rewrite prompt cannot be empty"
                if LLM_API_MODE == "openai":
                    rewritten = await rewrite_with_openai_compatible(app.state.http_client, text, prompt)
                else:
                    rewritten = await rewrite_with_ollama(app.state.http_client, text, prompt)
                return rewritten, None

            return None, "Unsupported job kind"
    except HTTPException as exc:
        logger.exception("NLP job execution failed")
        return None, build_llm_error_detail(exc)
    except httpx.HTTPError as exc:
        logger.exception("NLP job execution failed")
        return None, build_llm_error_detail(exc)
    except Exception as exc:
        logger.exception("NLP job execution failed unexpectedly")
        return None, build_llm_error_detail(exc)


async def publish_result(job_id: str, status: str, result: str | None = None, error: str | None = None) -> None:
    app.state.jobs[job_id] = {
        "status": status,
        "result": result,
        "error": error,
    }
    producer: AIOKafkaProducer = app.state.kafka_producer
    await producer.send_and_wait(
        NLP_JOB_RESULT_TOPIC,
        {
            "job_id": job_id,
            "status": status,
            "result": result,
            "error": error,
        },
    )


async def job_worker_loop() -> None:
    consumer: AIOKafkaConsumer = app.state.kafka_consumer
    await consumer.start()
    try:
        async for msg in consumer:
            payload: dict[str, Any] = msg.value
            job_id = str(payload.get("job_id", "")).strip()
            if not job_id:
                continue

            await publish_result(job_id, "processing")
            result, error = await process_job(payload)
            if error:
                await publish_result(job_id, "failed", error=error)
            else:
                await publish_result(job_id, "completed", result=result)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Kafka job worker loop crashed")


async def enqueue_job(kind: str, text: str, prompt: str | None = None) -> str:
    job_id = uuid.uuid4().hex
    app.state.jobs[job_id] = {"status": "queued", "result": None, "error": None}
    producer: AIOKafkaProducer = app.state.kafka_producer
    await producer.send_and_wait(
        NLP_JOB_REQUEST_TOPIC,
        {
            "job_id": job_id,
            "kind": kind,
            "text": text,
            "prompt": prompt,
        },
    )
    return job_id


@app.post("/api/jobs/summarize", response_model=JobAcceptedResponse)
async def enqueue_summarize_job(request: SummarizeRequest) -> JobAcceptedResponse:
    if not NLP_ASYNC_ENABLED:
        raise HTTPException(status_code=404, detail="Async NLP queue is not enabled")

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Transcript text cannot be empty")

    job_id = await enqueue_job("summarize", text)
    return JobAcceptedResponse(job_id=job_id, status="queued")


@app.post("/api/jobs/rewrite", response_model=JobAcceptedResponse)
async def enqueue_rewrite_job(request: RewriteRequest) -> JobAcceptedResponse:
    if not NLP_ASYNC_ENABLED:
        raise HTTPException(status_code=404, detail="Async NLP queue is not enabled")

    text = request.text.strip()
    instruction = request.prompt.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Transcript text cannot be empty")
    if not instruction:
        raise HTTPException(status_code=400, detail="Rewrite prompt cannot be empty")

    job_id = await enqueue_job("rewrite", text, instruction)
    return JobAcceptedResponse(job_id=job_id, status="queued")


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    if not NLP_ASYNC_ENABLED:
        raise HTTPException(status_code=404, detail="Async NLP queue is not enabled")

    state = app.state.jobs.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        job_id=job_id,
        status=str(state.get("status", "unknown")),
        result=state.get("result"),
        error=state.get("error"),
    )


@app.post("/api/summarize", response_model=SummarizeResponse)
async def summarize(request: SummarizeRequest) -> SummarizeResponse:
    if not bool(getattr(app.state, "model_ready", False)):
        raise HTTPException(status_code=503, detail="NLP model is not ready yet")

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Transcript text cannot be empty")

    try:
        async with app.state.nlp_limiter:
            if LLM_API_MODE == "openai":
                summary = await summarize_with_openai_compatible(app.state.http_client, text)
            else:
                summary = await summarize_with_ollama(app.state.http_client, text)
    except httpx.HTTPError as exc:
        logger.exception("Summarization request failed")
        raise HTTPException(status_code=502, detail=build_llm_error_detail(exc)) from exc

    return SummarizeResponse(summary=summary)


@app.post("/api/rewrite", response_model=RewriteResponse)
async def rewrite_transcript(request: RewriteRequest) -> RewriteResponse:
    if not bool(getattr(app.state, "model_ready", False)):
        raise HTTPException(status_code=503, detail="NLP model is not ready yet")

    text = request.text.strip()
    instruction = request.prompt.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Transcript text cannot be empty")
    if not instruction:
        raise HTTPException(status_code=400, detail="Rewrite prompt cannot be empty")

    try:
        async with app.state.nlp_limiter:
            if LLM_API_MODE == "openai":
                rewritten_text = await rewrite_with_openai_compatible(app.state.http_client, text, instruction)
            else:
                rewritten_text = await rewrite_with_ollama(app.state.http_client, text, instruction)
    except httpx.HTTPError as exc:
        logger.exception("Rewrite request failed")
        raise HTTPException(status_code=502, detail=build_llm_error_detail(exc)) from exc

    return RewriteResponse(rewritten_text=rewritten_text)
