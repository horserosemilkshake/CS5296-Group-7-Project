import asyncio
import audioop
import json
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from vosk import KaldiRecognizer, Model

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "/app/models/vosk-model-small-en-us-0.15")
NLP_API_BASE = os.getenv("NLP_API_BASE", "http://nlp:8766")
NLP_ASYNC_ENABLED = os.getenv("NLP_ASYNC_ENABLED", "false").lower() == "true"
TARGET_SAMPLE_RATE = 16000
MAX_AUDIO_QUEUE_SIZE = max(int(os.getenv("MAX_AUDIO_QUEUE_SIZE", "32")), 4)
MAX_AUDIO_CHUNK_BYTES = max(int(os.getenv("MAX_AUDIO_CHUNK_BYTES", "262144")), 32768)
# When CHUNK_MERGE_ENABLED=true, adjacent silent chunks are merged into one
# before being fed to Vosk, reducing AcceptWaveform() call overhead.
CHUNK_MERGE_ENABLED = os.getenv("CHUNK_MERGE_ENABLED", "false").lower() == "true"
SILENCE_RMS_THRESHOLD = int(os.getenv("SILENCE_RMS_THRESHOLD", "200"))

app = FastAPI(title="Realtime Speech Transcription")
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


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
    result: str | None = None
    error: str | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: str | None = None
    error: str | None = None


class TranscriptionSession:
    def __init__(self, recognizer: KaldiRecognizer, source_rate: int) -> None:
        self.recognizer = recognizer
        self.source_rate = max(source_rate, 8000)
        self.rate_state: Optional[tuple] = None

    def process_chunk(self, chunk: bytes) -> dict:
        if self.source_rate != TARGET_SAMPLE_RATE:
            chunk, self.rate_state = audioop.ratecv(
                chunk,
                2,
                1,
                self.source_rate,
                TARGET_SAMPLE_RATE,
                self.rate_state,
            )

        if self.recognizer.AcceptWaveform(chunk):
            result = json.loads(self.recognizer.Result())
            return {"type": "final", "text": result.get("text", "")}

        partial = json.loads(self.recognizer.PartialResult())
        return {"type": "partial", "text": partial.get("partial", "")}

    def finish(self) -> dict:
        final_data = json.loads(self.recognizer.FinalResult())
        return {"type": "final", "text": final_data.get("text", "")}


class SilenceMerger:
    """Buffer and merge consecutive low-amplitude audio chunks before Vosk processing.

    When CHUNK_MERGE_ENABLED=true, chunks whose RMS energy is below
    SILENCE_RMS_THRESHOLD are held in an internal buffer instead of being
    dispatched immediately to AcceptWaveform().  The buffer is flushed as a
    single merged chunk the moment a louder (speech) chunk arrives, or when
    flush() is called at end-of-stream.  This reduces redundant Vosk calls
    for recordings that contain significant silence.

    Algorithm
    ---------
    For each raw PCM chunk fed via feed():
      - rms < threshold  →  append to silence buffer; return []  (no Vosk call)
      - rms >= threshold →  flush buffer as one merged chunk + emit speech chunk
    Call flush() after the last chunk to drain any remaining buffered silence.
    """

    def __init__(self, rms_threshold: int = 200) -> None:
        self.rms_threshold = rms_threshold
        self._buf: list[bytes] = []

    def feed(self, chunk: bytes) -> list[bytes]:
        """Return 0, 1, or 2 chunks that are ready for Vosk processing."""
        try:
            rms = audioop.rms(chunk, 2)
        except Exception:
            rms = self.rms_threshold  # treat malformed data as speech

        if rms < self.rms_threshold:
            self._buf.append(chunk)
            return []

        # Speech chunk: flush accumulated silence first, then emit this chunk.
        out: list[bytes] = []
        if self._buf:
            out.append(b"".join(self._buf))
            self._buf = []
        out.append(chunk)
        return out

    def flush(self) -> list[bytes]:
        """Drain any remaining buffered silence at end-of-stream."""
        if self._buf:
            merged = b"".join(self._buf)
            self._buf = []
            return [merged]
        return []


def load_model() -> Model:
    if not os.path.isdir(VOSK_MODEL_PATH):
        raise RuntimeError(
            f"Vosk model path not found: {VOSK_MODEL_PATH}. "
            "Set VOSK_MODEL_PATH or include a model in your image."
        )
    logger.info("Loading Vosk model from %s", VOSK_MODEL_PATH)
    return Model(VOSK_MODEL_PATH)


@app.on_event("startup")
async def startup_event() -> None:
    app.state.vosk_model = load_model()
    app.state.http_client = httpx.AsyncClient(timeout=120)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    http_client: Optional[httpx.AsyncClient] = getattr(app.state, "http_client", None)
    if http_client is not None:
        await http_client.aclose()


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse("frontend/index.html")


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket) -> None:
    await websocket.accept()

    model: Model = app.state.vosk_model
    sample_rate = int(websocket.query_params.get("sample_rate", "48000"))
    recognizer = KaldiRecognizer(model, TARGET_SAMPLE_RATE)
    recognizer.SetWords(True)
    session = TranscriptionSession(recognizer, sample_rate)
    audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=MAX_AUDIO_QUEUE_SIZE)
    chunk_count = 0

    logger.info("Transcribe session started (source_rate=%s)", sample_rate)

    async def process_audio_queue() -> None:
        merger = SilenceMerger(SILENCE_RMS_THRESHOLD) if CHUNK_MERGE_ENABLED else None
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                # Flush any remaining silence before closing.
                if merger:
                    for merged_chunk in merger.flush():
                        event = await asyncio.to_thread(session.process_chunk, merged_chunk)
                        await websocket.send_json(event)
                break
            chunks_to_process = merger.feed(chunk) if merger else [chunk]
            for c in chunks_to_process:
                event = await asyncio.to_thread(session.process_chunk, c)
                await websocket.send_json(event)

    worker_task = asyncio.create_task(process_audio_queue())

    try:
        while True:
            message = await websocket.receive()
            message_type = message.get("type")

            if message_type == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                chunk_count += 1
                chunk = message["bytes"]
                if len(chunk) > MAX_AUDIO_CHUNK_BYTES:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Audio chunk too large",
                        }
                    )
                    break

                if chunk_count % 50 == 0:
                    try:
                        rms = audioop.rms(chunk, 2)
                        logger.debug("Audio RMS at chunk %s: %s", chunk_count, rms)
                    except Exception:
                        pass

                await audio_queue.put(chunk)
                continue

            text_data = message.get("text")
            if text_data:
                try:
                    payload = json.loads(text_data)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON control message"})
                    continue
                if payload.get("event") == "stop":
                    logger.info("Stop event received after %s chunks", chunk_count)
                    break

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected after %s chunks", chunk_count)
    except Exception as exc:
        logger.exception("Transcription websocket error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            if not worker_task.done():
                try:
                    audio_queue.put_nowait(None)
                except asyncio.QueueFull:
                    worker_task.cancel()

            try:
                await worker_task
            except Exception:
                # Worker may fail if the client disconnects while a send is in flight.
                pass

            final_event = await asyncio.to_thread(session.finish)
            if final_event.get("text"):
                logger.info("Final transcript at session end: %s", final_event["text"])
                await websocket.send_json(final_event)
            await websocket.send_json({"type": "session_complete"})
            logger.info("Transcribe session complete")
        except Exception:
            pass

        try:
            await websocket.close()
        except Exception:
            pass


async def call_nlp_service(path: str, payload: dict) -> dict:
    client: httpx.AsyncClient = app.state.http_client
    response = await client.post(
        f"{NLP_API_BASE.rstrip('/')}{path}",
        json=payload,
    )

    response.raise_for_status()
    return response.json()


async def call_nlp_service_get(path: str) -> dict:
    client: httpx.AsyncClient = app.state.http_client
    response = await client.get(f"{NLP_API_BASE.rstrip('/')}{path}")
    response.raise_for_status()
    return response.json()


@app.post("/api/summarize", response_model=JobAcceptedResponse)
async def summarize(request: SummarizeRequest) -> JobAcceptedResponse:
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Transcript text cannot be empty")

    try:
        if NLP_ASYNC_ENABLED:
            payload = await call_nlp_service("/api/jobs/summarize", {"text": text})
            job_id = str(payload.get("job_id", "")).strip()
            if not job_id:
                raise HTTPException(status_code=502, detail="NLP service did not return job id")
            return JobAcceptedResponse(job_id=job_id, status="queued")

        payload = await call_nlp_service("/api/summarize", {"text": text})
        summary = payload.get("summary", "").strip()
        if not summary:
            raise HTTPException(status_code=502, detail="NLP service returned empty summary")
        return JobAcceptedResponse(job_id="inline", status="completed", result=summary)
    except httpx.HTTPStatusError as exc:
        logger.exception("Summarization request failed with NLP status error")
        detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=f"NLP service error: {detail}") from exc
    except httpx.HTTPError as exc:
        logger.exception("Summarization request failed")
        raise HTTPException(status_code=502, detail=f"Failed to reach NLP service: {exc}") from exc



@app.post("/api/rewrite", response_model=JobAcceptedResponse)
async def rewrite_transcript(request: RewriteRequest) -> JobAcceptedResponse:
    text = request.text.strip()
    instruction = request.prompt.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Transcript text cannot be empty")
    if not instruction:
        raise HTTPException(status_code=400, detail="Rewrite prompt cannot be empty")

    try:
        if NLP_ASYNC_ENABLED:
            payload = await call_nlp_service(
                "/api/jobs/rewrite",
                {"text": text, "prompt": instruction},
            )
            job_id = str(payload.get("job_id", "")).strip()
            if not job_id:
                raise HTTPException(status_code=502, detail="NLP service did not return job id")
            return JobAcceptedResponse(job_id=job_id, status="queued")

        payload = await call_nlp_service(
            "/api/rewrite",
            {"text": text, "prompt": instruction},
        )
        rewritten_text = payload.get("rewritten_text", "").strip()
        if not rewritten_text:
            raise HTTPException(status_code=502, detail="NLP service returned empty rewrite")
        return JobAcceptedResponse(job_id="inline", status="completed", result=rewritten_text)
    except httpx.HTTPStatusError as exc:
        logger.exception("Rewrite request failed with NLP status error")
        detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=f"NLP service error: {detail}") from exc
    except httpx.HTTPError as exc:
        logger.exception("Rewrite request failed")
        raise HTTPException(status_code=502, detail=f"Failed to reach NLP service: {exc}") from exc



@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    if not NLP_ASYNC_ENABLED:
        raise HTTPException(status_code=404, detail="Async job status is not enabled")

    if not job_id.strip():
        raise HTTPException(status_code=400, detail="Job id is required")

    try:
        payload = await call_nlp_service_get(f"/api/jobs/{job_id}")
    except httpx.HTTPStatusError as exc:
        logger.exception("Job status request failed with NLP status error")
        detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=f"NLP service error: {detail}") from exc
    except httpx.HTTPError as exc:
        logger.exception("Job status request failed")
        raise HTTPException(status_code=502, detail=f"Failed to reach NLP service: {exc}") from exc

    return JobStatusResponse(
        job_id=str(payload.get("job_id", "")),
        status=str(payload.get("status", "unknown")),
        result=payload.get("result"),
        error=payload.get("error"),
    )
