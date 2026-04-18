#!/usr/bin/env python3
"""Concurrency stress test for backend APIs and websocket transcription.

This script runs high-concurrency scenarios against gateway endpoints and captures:
- request/session latency, error, and throughput stats
- overload/backpressure indicators (timeouts, 5xx/429 rates, p95 growth)
- sampled CPU/memory metrics from Docker or Kubernetes (when available)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import random
import re
import ssl
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_PROMPT = "Rewrite this transcript to be concise and clear."
DEFAULT_TEXT = (
    "We discussed architecture tasks, release risk mitigation, deployment sequencing, "
    "and ownership tracking for launch readiness."
)


@dataclass
class RequestResult:
    endpoint: str
    status: int
    latency_ms: float
    ok: bool
    error: str = ""
    queue_wait_ms: float = 0.0
    # Populated for WebSocket transcribe sessions only.
    chunks_original: int = 0  # chunks before client-side silence merging
    chunks_sent: int = 0      # chunks actually transmitted


class ResourceMonitor:
    def __init__(self, mode: str, namespace: str, sample_interval: float) -> None:
        self.mode = mode
        self.namespace = namespace
        self.sample_interval = sample_interval
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run_cmd(self, cmd: list[str]) -> tuple[int, str, str]:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def _detect_mode(self) -> str:
        if self.mode != "auto":
            return self.mode

        rc, _, _ = self._run_cmd(["kubectl", "get", "pods", "-n", self.namespace])
        if rc == 0:
            return "k8s"

        rc, _, _ = self._run_cmd(["docker", "ps"])
        if rc == 0:
            return "docker"

        return "none"

    def start(self) -> None:
        self.mode = self._detect_mode()
        if self.mode == "none":
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            ts = time.time()
            snapshot: dict[str, Any] = {}
            if self.mode == "docker":
                snapshot = self._sample_docker()
            elif self.mode == "k8s":
                snapshot = self._sample_k8s()

            if snapshot:
                snapshot["timestamp"] = ts
                self.samples.append(snapshot)
            time.sleep(self.sample_interval)

    def _parse_mem_to_mib(self, text: str) -> float:
        text = text.strip()
        m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMG]i?B)$", text)
        if not m:
            return 0.0
        value = float(m.group(1))
        unit = m.group(2)
        factors = {
            "KiB": 1 / 1024,
            "MiB": 1,
            "GiB": 1024,
            "KB": 1 / 1000,
            "MB": 1 / 1.048576,
            "GB": 1000 / 1.048576,
        }
        return value * factors.get(unit, 0.0)

    def _normalize_service_name(self, name: str) -> str:
        prefixes = ["gateway", "nlp", "ollama", "nginx", "prometheus", "grafana", "cadvisor"]
        for pref in prefixes:
            if name.startswith(pref) or f"-{pref}-" in name or name.startswith(f"realtime-asr-{pref}"):
                return pref
        return "other"

    def _sample_docker(self) -> dict[str, Any]:
        rc, out, _ = self._run_cmd(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"]
        )
        if rc != 0 or not out:
            return {}

        aggregate: dict[str, dict[str, float]] = defaultdict(lambda: {"cpu": 0.0, "mem_mib": 0.0})
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) != 3:
                continue
            name, cpu_str, mem_str = parts
            service = self._normalize_service_name(name)
            cpu = float(cpu_str.replace("%", "").strip() or 0)
            mem_used = mem_str.split("/")[0].strip()
            mem_mib = self._parse_mem_to_mib(mem_used)
            aggregate[service]["cpu"] += cpu
            aggregate[service]["mem_mib"] += mem_mib

        return {"mode": "docker", "services": aggregate}

    def _parse_k8s_cpu(self, text: str) -> float:
        text = text.strip()
        if text.endswith("m"):
            return float(text[:-1]) / 1000.0
        try:
            return float(text)
        except ValueError:
            return 0.0

    def _sample_k8s(self) -> dict[str, Any]:
        rc, out, _ = self._run_cmd(["kubectl", "top", "pods", "-n", self.namespace, "--no-headers"])
        if rc != 0 or not out:
            return {}

        aggregate: dict[str, dict[str, float]] = defaultdict(lambda: {"cpu": 0.0, "mem_mib": 0.0})
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            pod_name, cpu_text, mem_text = parts[0], parts[1], parts[2]
            service = self._normalize_service_name(pod_name)
            aggregate[service]["cpu"] += self._parse_k8s_cpu(cpu_text)
            aggregate[service]["mem_mib"] += self._parse_mem_to_mib(mem_text)

        return {"mode": "k8s", "services": aggregate}


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def get_ecs_public_ip() -> str:
    # Cloud metadata endpoint.
    try:
        with urllib.request.urlopen("http://100.100.100.200/latest/meta-data/eipv4", timeout=2) as resp:
            ip_text = resp.read().decode("utf-8").strip()
            if ip_text:
                return ip_text
    except Exception:  # noqa: BLE001
        pass
    return ""


def discover_k8s_gateway_urls(namespace: str) -> list[str]:
    urls: list[str] = []
    rc, lb_ip, _ = run_cmd(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            "gateway",
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].ip}",
        ]
    )
    if rc == 0 and lb_ip:
        urls.append(f"http://{lb_ip}")

    rc, node_port, _ = run_cmd(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            "gateway",
            "-o",
            "jsonpath={.spec.ports[0].nodePort}",
        ]
    )
    if rc == 0 and node_port:
        ecs_ip = get_ecs_public_ip()
        if ecs_ip:
            urls.append(f"http://{ecs_ip}:{node_port}")
        urls.append(f"http://127.0.0.1:{node_port}")
        urls.append(f"http://localhost:{node_port}")

    return urls


def url_reachable(base_url: str, timeout: float, ssl_context: ssl.SSLContext | None) -> bool:
    probe_url = f"{base_url.rstrip('/')}/"
    req = urllib.request.Request(probe_url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=min(timeout, 5.0), context=ssl_context) as resp:
            return 200 <= resp.status < 500
    except Exception:  # noqa: BLE001
        return False


def resolve_base_url(base_url: str, namespace: str, timeout: float, ssl_context: ssl.SSLContext | None) -> str:
    if base_url != "auto":
        return base_url.rstrip("/")

    candidates = [
        "http://localhost:8765",
        "http://127.0.0.1:8765",
    ]
    candidates.extend(discover_k8s_gateway_urls(namespace))

    # Keep order but remove duplicates.
    seen: set[str] = set()
    deduped = [u for u in candidates if not (u in seen or seen.add(u))]

    for candidate in deduped:
        if url_reachable(candidate, timeout, ssl_context):
            return candidate

    # Fall back to the first candidate for clearer downstream errors.
    return deduped[0] if deduped else "http://localhost:8765"


def make_request(
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    timeout: float,
    ssl_context: ssl.SSLContext | None,
) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            resp.read()
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTPError: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def to_ws_base_url(http_base_url: str) -> str:
    parsed = urlparse(http_base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}".rstrip("/")


def load_audio_chunks_from_file(path: str, chunk_size: int, target_sample_rate: int) -> list[bytes]:
    """Read a WAV file and return raw 16-bit mono PCM chunks at target_sample_rate.

    Uses only stdlib (wave, audioop) — no extra dependencies required.
    """
    import audioop
    import wave

    try:
        with wave.open(path, "rb") as wf:
            src_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
    except Exception as exc:
        raise SystemExit(f"Cannot read audio file '{path}': {exc}") from exc

    # Mix down to mono.
    if n_channels > 1:
        raw = audioop.tomono(raw, sampwidth, 0.5, 0.5)

    # Ensure 16-bit PCM (Vosk expects s16le).
    if sampwidth != 2:
        raw = audioop.lin2lin(raw, sampwidth, 2)
        sampwidth = 2

    # Resample if the file's rate differs from the WebSocket session rate.
    if src_rate != target_sample_rate:
        raw, _ = audioop.ratecv(raw, sampwidth, 1, src_rate, target_sample_rate, None)

    # Split into fixed-size chunks, padding the last one.
    chunks: list[bytes] = []
    for i in range(0, len(raw), chunk_size):
        chunk = raw[i : i + chunk_size]
        if len(chunk) < chunk_size:
            chunk = chunk + bytes(chunk_size - len(chunk))
        chunks.append(chunk)

    if not chunks:
        raise SystemExit(f"Audio file '{path}' produced no chunks (file may be empty).")

    return chunks


def merge_low_entropy_chunks(
    chunks: list[bytes],
    rms_threshold: int,
    sampwidth: int = 2,
) -> list[bytes]:
    """Merge adjacent low-RMS (silent / low-entropy) PCM chunks into larger ones.

    Adjacent chunks whose RMS energy falls below *rms_threshold* are concatenated
    into a single larger chunk.  When a louder (speech) chunk arrives the
    accumulated silence is flushed first as one merged chunk, then the speech
    chunk is emitted on its own.  Trailing silence is also flushed at the end.

    This mirrors the server-side SilenceMerger / CHUNK_MERGE_ENABLED logic and
    lets the stress-test exercise *client-side* pre-merging before frames are
    sent over WebSocket.  That reduces both the WS frame count and — unless the
    server also merges — the number of Vosk AcceptWaveform() calls received.

    Comparison workflow
    -------------------
    Baseline:  no flag           → N chunks sent one-by-one
    Client merge: --chunk-merge-threshold RMS  → fewer merged frames sent
    Server merge: CHUNK_MERGE_ENABLED=true     → server buffers silence in queue
    Both combined:               → two-level reduction

    Args:
        chunks:        16-bit mono PCM byte chunks (s16le), any fixed size.
        rms_threshold: RMS amplitude (0-32767) below which a chunk is silence.
                       Typical values: 50 (very quiet) – 500 (aggressively merge).
        sampwidth:     Sample width in bytes; 2 for 16-bit PCM.

    Returns:
        Merged list — same or fewer entries than the input.
    """
    import audioop

    merged: list[bytes] = []
    silence_buf: list[bytes] = []

    for chunk in chunks:
        try:
            rms = audioop.rms(chunk, sampwidth)
        except Exception:
            rms = rms_threshold  # treat malformed chunks as speech

        if rms < rms_threshold:
            silence_buf.append(chunk)
        else:
            if silence_buf:
                merged.append(b"".join(silence_buf))
                silence_buf = []
            merged.append(chunk)

    # Flush any trailing silence as a single merged chunk.
    if silence_buf:
        merged.append(b"".join(silence_buf))

    return merged


def capture_mic_audio(duration_sec: float, sample_rate: int, chunk_size: int) -> list[bytes]:
    """Record from the default microphone and return PCM chunks.

    The clip is recorded once and then reused across all WebSocket sessions,
    which keeps the stress-test reproducible and avoids device contention.

    Requires: pip install sounddevice numpy
    """
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise SystemExit(
            "Microphone capture requires extra packages: pip install sounddevice numpy"
        ) from exc

    print(f"[mic] Recording {duration_sec:.1f}s from default microphone at {sample_rate} Hz ...")
    recording = sd.rec(
        int(duration_sec * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    raw: bytes = recording.tobytes()

    chunks: list[bytes] = []
    for i in range(0, len(raw), chunk_size):
        chunk = raw[i : i + chunk_size]
        if len(chunk) < chunk_size:
            chunk = chunk + bytes(chunk_size - len(chunk))
        chunks.append(chunk)

    print(f"[mic] Captured {len(chunks)} chunks ({len(raw):,} bytes total).")
    return chunks


async def run_ws_session(
    *,
    ws_base_url: str,
    sample_rate: int,
    chunk_count: int,
    chunk_size: int,
    chunk_delay_ms: float,
    timeout: float,
    ssl_context: ssl.SSLContext | None,
    audio_chunks: list[bytes] | None = None,
    chunk_merge_threshold: int | None = None,
) -> tuple[int, str, int, int]:
    """Run one WebSocket transcription session.

    Returns:
        (status, error, original_chunk_count, sent_chunk_count)
        When chunk_merge_threshold is None, sent == original (baseline).
    """
    import websockets

    ws_url = f"{ws_base_url}/ws/transcribe?sample_rate={sample_rate}"
    # Real audio when provided; silence otherwise.
    chunks_to_send: list[bytes] = audio_chunks if audio_chunks else [bytes(chunk_size)] * chunk_count
    original_count = len(chunks_to_send)

    # Optionally merge adjacent silent chunks before sending.
    # Compare against the baseline (no flag) to measure overhead reduction.
    if chunk_merge_threshold is not None:
        chunks_to_send = merge_low_entropy_chunks(chunks_to_send, chunk_merge_threshold)
    sent_count = len(chunks_to_send)

    try:
        async with websockets.connect(ws_url, open_timeout=timeout, close_timeout=2, ssl=ssl_context) as ws:
            for chunk in chunks_to_send:
                await ws.send(chunk)
                if chunk_delay_ms > 0:
                    await asyncio.sleep(chunk_delay_ms / 1000.0)

            await ws.send(json.dumps({"event": "stop"}))

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                wait_left = max(0.05, deadline - time.monotonic())
                msg = await asyncio.wait_for(ws.recv(), timeout=wait_left)
                if isinstance(msg, bytes):
                    continue
                try:
                    payload = json.loads(msg)
                except json.JSONDecodeError:
                    continue

                msg_type = payload.get("type")
                if msg_type == "session_complete":
                    return 101, "", original_count, sent_count
                if msg_type == "error":
                    return 0, payload.get("message", "websocket error"), original_count, sent_count

            return 0, "timeout waiting for session_complete", original_count, sent_count
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc), original_count, sent_count


def run_scenario(
    *,
    name: str,
    base_url: str,
    concurrency: int,
    total_requests: int,
    timeout: float,
    seed: int,
    ws_base_url: str,
    ws_sample_rate: int,
    ws_chunk_count: int,
    ws_chunk_size: int,
    ws_chunk_delay_ms: float,
    ssl_context: ssl.SSLContext | None,
    audio_chunks: list[bytes] | None = None,
    chunk_merge_threshold: int | None = None,
) -> dict[str, Any]:
    q: queue.Queue[tuple[float, int, str]] = queue.Queue()
    results: list[RequestResult] = []
    lock = threading.Lock()
    rng = random.Random(seed)

    for i in range(total_requests):
        if name == "summarize":
            endpoint = "/api/summarize"
        elif name == "rewrite":
            endpoint = "/api/rewrite"
        elif name == "transcribe":
            endpoint = "/ws/transcribe"
        elif name == "mixed-all":
            roll = rng.random()
            endpoint = "/api/summarize" if roll < 0.34 else ("/api/rewrite" if roll < 0.67 else "/ws/transcribe")
        else:
            endpoint = "/api/rewrite" if rng.random() < 0.5 else "/api/summarize"
        q.put((time.perf_counter(), i, endpoint))

    def worker() -> None:
        while True:
            try:
                enqueued_at, req_id, endpoint = q.get_nowait()
            except queue.Empty:
                return

            started_at = time.perf_counter()
            queue_wait_ms = (started_at - enqueued_at) * 1000.0

            n_orig = n_sent = 0
            if endpoint == "/ws/transcribe":
                status, error, n_orig, n_sent = asyncio.run(
                    run_ws_session(
                        ws_base_url=ws_base_url,
                        sample_rate=ws_sample_rate,
                        chunk_count=ws_chunk_count,
                        chunk_size=ws_chunk_size,
                        chunk_delay_ms=ws_chunk_delay_ms,
                        timeout=timeout,
                        ssl_context=ssl_context,
                        audio_chunks=audio_chunks,
                        chunk_merge_threshold=chunk_merge_threshold,
                    )
                )
            else:
                payload = {"text": f"{DEFAULT_TEXT} [req={req_id}]"}
                if endpoint == "/api/rewrite":
                    payload["prompt"] = DEFAULT_PROMPT
                status, error = make_request(base_url, endpoint, payload, timeout, ssl_context)

            latency_ms = (time.perf_counter() - started_at) * 1000.0
            ok = 200 <= status < 300 or status == 101

            with lock:
                results.append(
                    RequestResult(
                        endpoint=endpoint,
                        status=status,
                        latency_ms=latency_ms,
                        ok=ok,
                        error=error,
                        queue_wait_ms=queue_wait_ms,
                        chunks_original=n_orig,
                        chunks_sent=n_sent,
                    )
                )

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    duration = time.perf_counter() - started

    latencies = sorted(r.latency_ms for r in results)
    queue_waits = sorted(r.queue_wait_ms for r in results)
    statuses = Counter(r.status for r in results)
    endpoint_counts = Counter(r.endpoint for r in results)

    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count
    timeout_count = sum(1 for r in results if r.status == 0)
    overload_count = sum(1 for r in results if r.status in (429, 500, 502, 503, 504) or r.status == 0)

    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)
    throughput = len(results) / duration if duration > 0 else 0.0

    # Backpressure index is a practical signal for overload under parallel load.
    # Scale 0..100, combining p95 latency growth and overload error rates.
    backpressure_index = min(
        100.0,
        (overload_count / max(1, len(results))) * 70.0 + (min(p95, 5000.0) / 5000.0) * 30.0,
    )

    # Chunk-merge statistics for WebSocket transcription sessions.
    ws_results = [r for r in results if r.endpoint == "/ws/transcribe"]
    _total_orig = sum(r.chunks_original for r in ws_results)
    _total_sent = sum(r.chunks_sent for r in ws_results)
    chunk_merge_stats: dict[str, Any] = {
        "enabled": chunk_merge_threshold is not None,
        "threshold_rms": chunk_merge_threshold,
        "sessions": len(ws_results),
        "total_chunks_original": _total_orig,
        "total_chunks_sent": _total_sent,
        # compression_ratio < 1.0 means fewer frames were sent (good).
        "compression_ratio": round(_total_sent / _total_orig, 3) if _total_orig else 1.0,
        "reduction_pct": round((1.0 - _total_sent / _total_orig) * 100, 1) if _total_orig else 0.0,
    }

    return {
        "scenario": name,
        "concurrency": concurrency,
        "requests": len(results),
        "duration_sec": round(duration, 3),
        "throughput_rps": round(throughput, 2),
        "success": ok_count,
        "failed": fail_count,
        "timeouts": timeout_count,
        "overload_errors": overload_count,
        "status_counts": dict(statuses),
        "endpoint_counts": dict(endpoint_counts),
        "latency_ms": {
            "avg": round(statistics.fmean(latencies), 2) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(p95, 2),
            "p99": round(p99, 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "queue_wait_ms": {
            "avg": round(statistics.fmean(queue_waits), 2) if queue_waits else 0.0,
            "p95": round(percentile(queue_waits, 0.95), 2),
            "max": round(max(queue_waits), 2) if queue_waits else 0.0,
        },
        "backpressure_index": round(backpressure_index, 2),
        "sample_errors": [r.error for r in results if r.error][:5],
        "chunk_merge": chunk_merge_stats,
    }


def summarize_resource_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_service: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"cpu": [], "mem_mib": []})
    mode = "none"

    for sample in samples:
        mode = sample.get("mode", mode)
        services = sample.get("services", {})
        for service, metrics in services.items():
            by_service[service]["cpu"].append(float(metrics.get("cpu", 0.0)))
            by_service[service]["mem_mib"].append(float(metrics.get("mem_mib", 0.0)))

    summary = {"mode": mode, "services": {}}
    for service, metrics in by_service.items():
        cpu_vals = metrics["cpu"]
        mem_vals = metrics["mem_mib"]
        summary["services"][service] = {
            "cpu_avg": round(statistics.fmean(cpu_vals), 3) if cpu_vals else 0.0,
            "cpu_max": round(max(cpu_vals), 3) if cpu_vals else 0.0,
            "mem_mib_avg": round(statistics.fmean(mem_vals), 2) if mem_vals else 0.0,
            "mem_mib_max": round(max(mem_vals), 2) if mem_vals else 0.0,
        }

    return summary


def print_table(results: list[dict[str, Any]]) -> None:
    header = ("scenario", "conc", "req", "ok", "fail", "rps", "p95(ms)", "p99(ms)", "bp_index")
    print("\n" + " | ".join(header))
    print("-" * 92)
    for r in results:
        print(
            f"{r['scenario']:<9} | {r['concurrency']:<4} | {r['requests']:<4} | {r['success']:<4} | "
            f"{r['failed']:<4} | {r['throughput_rps']:<6} | {r['latency_ms']['p95']:<7} | "
            f"{r['latency_ms']['p99']:<7} | {r['backpressure_index']:<8}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stress test summarize/rewrite/transcribe under high concurrency")
    parser.add_argument(
        "--base-url",
        default="auto",
        help="Gateway base URL. Use 'auto' to detect local/k8s/ECS gateway endpoint.",
    )
    parser.add_argument(
        "--scenarios",
        default="summarize,rewrite,mixed,transcribe",
        help="Comma list: summarize,rewrite,mixed,transcribe,mixed-all",
    )
    parser.add_argument("--concurrency", default="1,4,8,16,32", help="Comma list of thread counts")
    parser.add_argument("--requests-per-scenario", type=int, default=60, help="Requests per scenario/concurrency")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-request/session timeout in seconds")
    parser.add_argument("--monitor-mode", choices=["auto", "docker", "k8s", "none"], default="auto")
    parser.add_argument("--k8s-namespace", default="realtime-asr")
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="Disable TLS cert verification for HTTPS/WSS (useful for self-signed certs on ECS).",
    )
    parser.add_argument("--ws-sample-rate", type=int, default=48000)
    parser.add_argument("--ws-chunks", type=int, default=24, help="Audio chunks per websocket session")
    parser.add_argument("--ws-chunk-bytes", type=int, default=3200, help="Chunk size sent for websocket audio payload")
    parser.add_argument("--ws-chunk-delay-ms", type=float, default=8.0, help="Delay between websocket audio chunks")
    parser.add_argument(
        "--audio-file",
        default="",
        metavar="PATH",
        help="WAV file to stream as audio in WebSocket transcription tests (replaces silence). "
             "The file is resampled and converted to mono 16-bit PCM automatically.",
    )
    parser.add_argument(
        "--use-mic",
        action="store_true",
        help="Record a clip from the default microphone once and reuse it across all WebSocket sessions. "
             "Requires: pip install sounddevice numpy",
    )
    parser.add_argument(
        "--mic-duration",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Duration (seconds) to record from the microphone when --use-mic is set. Default: 5.0",
    )
    parser.add_argument(
        "--chunk-merge-threshold",
        type=int,
        default=None,
        metavar="RMS",
        help=(
            "RMS amplitude threshold (0-32767) for client-side silence merging. "
            "Adjacent chunks with RMS below this value are concatenated into one "
            "before being sent over WebSocket, reducing frame count and server-side "
            "Vosk AcceptWaveform() calls. Typical values: 100-500. "
            "Omit to disable (one-by-one baseline). "
            "Pair with CHUNK_MERGE_ENABLED=true on the server for server-side comparison."
        ),
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--nlp-semaphore",
        type=int,
        default=None,
        metavar="N",
        help="Optional metadata tag: SUMMARIZE_MAX_CONCURRENCY value used in this run.",
    )
    parser.add_argument(
        "--sweep-semaphores",
        default="",
        metavar="CSV",
        help=(
            "Run a semaphore sweep: comma-separated SUMMARIZE_MAX_CONCURRENCY values to test in sequence "
            "(e.g. '1,2,4,8'). For each value the script sets the env var on deployment/nlp via kubectl, "
            "waits for rollout, then runs the full stress matrix. "
            "Results are saved to --sweep-output-dir. Requires kubectl access to the cluster."
        ),
    )
    parser.add_argument(
        "--sweep-output-dir",
        default="test_results/semaphore-sweep",
        metavar="DIR",
        help="Directory for per-semaphore JSON reports when --sweep-semaphores is used. Default: test_results/semaphore-sweep",
    )
    parser.add_argument(
        "--sweep-skip-set-env",
        action="store_true",
        help=(
            "Skip the 'kubectl set env' + rollout step during a semaphore sweep. "
            "Use this when the cluster is not available or the env var is managed externally. "
            "Each iteration still runs the full stress matrix and tags the report with the semaphore value."
        ),
    )
    parser.add_argument(
        "--output",
        default=f"stress-results-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json",
        help="Path to write JSON report",
    )
    return parser.parse_args()


def run_stress_test(
    args: argparse.Namespace,
    ssl_context: ssl.SSLContext | None,
    scenarios: list[str],
    concurrencies: list[int],
    resolved_base_url: str,
    ws_base_url: str,
    audio_chunks: list[bytes] | None,
    chunk_merge_threshold: int | None,
    nlp_semaphore: int | None,
    output_path: Path,
) -> dict[str, Any]:
    """Run the full stress-test matrix and save results to *output_path*."""
    monitor = ResourceMonitor(mode=args.monitor_mode, namespace=args.k8s_namespace, sample_interval=args.sample_interval)

    full_report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "base_url": resolved_base_url,
            "ws_base_url": ws_base_url,
            "scenarios": scenarios,
            "concurrency": concurrencies,
            "requests_per_scenario": args.requests_per_scenario,
            "timeout_sec": args.timeout,
            "monitor_mode": args.monitor_mode,
            "k8s_namespace": args.k8s_namespace,
            "sample_interval_sec": args.sample_interval,
            "ws_sample_rate": args.ws_sample_rate,
            "ws_chunks": args.ws_chunks,
            "ws_chunk_bytes": args.ws_chunk_bytes,
            "ws_chunk_delay_ms": args.ws_chunk_delay_ms,
            "audio_source": args.audio_file or ("mic" if args.use_mic else "silence"),
            "chunk_merge_threshold": chunk_merge_threshold,
            "nlp_semaphore": nlp_semaphore,
        },
        "results": [],
        "resources": {},
    }

    monitor.start()
    try:
        for scenario in scenarios:
            for concurrency in concurrencies:
                print(f"Running scenario={scenario} concurrency={concurrency} ...")
                result = run_scenario(
                    name=scenario,
                    base_url=resolved_base_url,
                    concurrency=concurrency,
                    total_requests=args.requests_per_scenario,
                    timeout=args.timeout,
                    seed=args.seed + concurrency,
                    ws_base_url=ws_base_url,
                    ws_sample_rate=args.ws_sample_rate,
                    ws_chunk_count=args.ws_chunks,
                    ws_chunk_size=args.ws_chunk_bytes,
                    ws_chunk_delay_ms=args.ws_chunk_delay_ms,
                    ssl_context=ssl_context,
                    audio_chunks=audio_chunks,
                    chunk_merge_threshold=chunk_merge_threshold,
                )
                full_report["results"].append(result)
    finally:
        monitor.stop()

    full_report["resources"] = summarize_resource_samples(monitor.samples)
    full_report["ended_at"] = datetime.now(timezone.utc).isoformat()

    print_table(full_report["results"])
    mode = full_report["resources"].get("mode", "none")
    print(f"\nResource monitor mode: {mode}")
    if full_report["resources"].get("services"):
        for service, data in full_report["resources"]["services"].items():
            print(
                f"- {service}: cpu_avg={data['cpu_avg']} cpu_max={data['cpu_max']} "
                f"mem_mib_avg={data['mem_mib_avg']} mem_mib_max={data['mem_mib_max']}"
            )
    else:
        print("- No resource samples captured (monitor unavailable or disabled).")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(full_report, indent=2), encoding="utf-8")
    print(f"\nSaved detailed report to: {output_path}")
    return full_report


def _set_nlp_semaphore(namespace: str, value: int) -> bool:
    """Set SUMMARIZE_MAX_CONCURRENCY on deployment/nlp and wait for rollout.

    Returns True if the env var was applied successfully, False if kubectl
    failed (e.g. namespace not found). In the failure case a warning is printed
    and the caller can decide whether to abort or continue with a tag-only run.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] kubectl -n {namespace} set env deployment/nlp SUMMARIZE_MAX_CONCURRENCY={value}")
    try:
        subprocess.run(
            ["kubectl", "-n", namespace, "set", "env", "deployment/nlp", f"SUMMARIZE_MAX_CONCURRENCY={value}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        print(f"  WARNING: kubectl set env failed — {stderr or exc}")
        print("  Proceeding with tag-only run (SUMMARIZE_MAX_CONCURRENCY not changed on cluster).")
        return False
    print(f"[{ts}] Waiting for rollout to complete ...")
    try:
        subprocess.run(
            ["kubectl", "-n", namespace, "rollout", "status", "deployment/nlp", "--timeout=600s"],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"  WARNING: rollout status failed — {exc}")
        print("  Proceeding anyway.")
        return False
    return True


def main() -> None:
    args = parse_args()
    ssl_context: ssl.SSLContext | None = None
    if args.insecure_tls:
        ssl_context = ssl._create_unverified_context()  # noqa: SLF001

    resolved_base_url = resolve_base_url(args.base_url, args.k8s_namespace, args.timeout, ssl_context)
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    concurrencies = [int(x.strip()) for x in args.concurrency.split(",") if x.strip()]
    ws_base_url = to_ws_base_url(resolved_base_url)

    print(f"Resolved base URL: {resolved_base_url}")
    if args.insecure_tls:
        print("TLS verification: disabled (--insecure-tls)")

    # Pre-load audio once; reused across every WebSocket session.
    audio_chunks: list[bytes] | None = None
    if any(s in {"transcribe", "mixed-all"} for s in scenarios):
        try:
            import websockets  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                "WebSocket test requested but `websockets` is not installed. Install it with: pip install websockets"
            ) from exc

        if args.audio_file:
            print(f"Loading audio file: {args.audio_file}")
            audio_chunks = load_audio_chunks_from_file(args.audio_file, args.ws_chunk_bytes, args.ws_sample_rate)
            print(f"  → {len(audio_chunks)} chunks × {args.ws_chunk_bytes} bytes at {args.ws_sample_rate} Hz")
        elif args.use_mic:
            audio_chunks = capture_mic_audio(args.mic_duration, args.ws_sample_rate, args.ws_chunk_bytes)
        else:
            print("WebSocket audio: silence (use --audio-file PATH.wav or --use-mic for real speech)")

    chunk_merge_threshold: int | None = args.chunk_merge_threshold
    if chunk_merge_threshold is not None:
        print(
            f"Chunk merge: enabled (RMS threshold={chunk_merge_threshold}) "
            "— adjacent silent chunks pre-merged before sending"
        )
    else:
        print(
            "Chunk merge: disabled (one-by-one baseline). "
            "Use --chunk-merge-threshold RMS to enable client-side merging, "
            "or set CHUNK_MERGE_ENABLED=true on the server for server-side merging."
        )

    # ── Semaphore sweep mode ─────────────────────────────────────────────────
    if args.sweep_semaphores:
        try:
            sweep_values = [int(x.strip()) for x in args.sweep_semaphores.split(",") if x.strip()]
        except ValueError as exc:
            raise SystemExit(f"--sweep-semaphores: invalid value — {exc}") from exc
        if not sweep_values:
            raise SystemExit("--sweep-semaphores: no values provided")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sweep_dir = Path(args.sweep_output_dir)
        sweep_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nSemaphore sweep: values={sweep_values}  output={sweep_dir}")

        for sem in sweep_values:
            if args.sweep_skip_set_env:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n[{ts}] Skipping kubectl set env (--sweep-skip-set-env). SUMMARIZE_MAX_CONCURRENCY={sem} recorded as tag only.")
            else:
                _set_nlp_semaphore(args.k8s_namespace, sem)
            out = sweep_dir / f"stress-semaphore-{sem}-{timestamp}.json"
            print(f"NLP semaphore (tag): {sem}")
            report = run_stress_test(
                args, ssl_context, scenarios, concurrencies,
                resolved_base_url, ws_base_url, audio_chunks,
                chunk_merge_threshold, sem, out,
            )
            results = report.get("results", [])
            errs = sum(int(r.get("failed", 0)) for r in results)
            reqs = sum(int(r.get("requests", 0)) for r in results)
            print(f"\n  \u25b8 semaphore={sem}  failed={errs} / requests={reqs}")

        print(f"\nSemaphore sweep complete: {sweep_dir}")
        return

    # ── Single-run mode ──────────────────────────────────────────────────────
    if args.nlp_semaphore is not None:
        print(f"NLP semaphore (tag): {args.nlp_semaphore}")
    output_path = Path(args.output)
    run_stress_test(
        args, ssl_context, scenarios, concurrencies,
        resolved_base_url, ws_base_url, audio_chunks,
        chunk_merge_threshold, args.nlp_semaphore, output_path,
    )


if __name__ == "__main__":
    main()
