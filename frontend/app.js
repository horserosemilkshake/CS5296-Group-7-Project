const startStopBtn = document.getElementById("startStopBtn");
const summarizeBtn = document.getElementById("summarizeBtn");
const downloadTranscriptBtn = document.getElementById("downloadTranscriptBtn");
const downloadSummaryBtn = document.getElementById("downloadSummaryBtn");
const rewriteBtn = document.getElementById("rewriteBtn");
const rewritePresetSelect = document.getElementById("rewritePresetSelect");
const transcriptText = document.getElementById("transcriptText");
const summarySection = document.getElementById("summarySection");
const summaryText = document.getElementById("summaryText");
const rewriteStatus = document.getElementById("rewriteStatus");
const rewriteDebug = document.getElementById("rewriteDebug");
const statusPill = document.getElementById("statusPill");

let ws = null;
let mediaStream = null;
let audioContext = null;
let processorNode = null;
let sourceNode = null;
let isRecording = false;
let isStopping = false;
let finalizedSegments = [];
let livePartial = "";
let transcriptOverride = "";
let stopTimeoutId = null;

function setRewriteDebug(message) {
  if (!rewriteDebug) {
    return;
  }
  const stamp = new Date().toLocaleTimeString();
  rewriteDebug.textContent = `Debug ${stamp}: ${message}`;
}

function setStatus(mode, label) {
  statusPill.classList.remove("idle", "recording", "processing");
  statusPill.classList.add(mode);
  statusPill.textContent = label;
}

function updateTranscriptView() {
  const complete = getCurrentTranscript();
  transcriptText.textContent = complete || "Your transcript will appear here...";
}

function getCurrentTranscript() {
  if (transcriptOverride.trim()) {
    return transcriptOverride.trim();
  }

  const joinedFinal = finalizedSegments.join(" ").trim();
  return `${joinedFinal} ${livePartial}`.trim();
}

function getTranscriptForActions() {
  const textFromState = getCurrentTranscript();
  if (textFromState) {
    return textFromState;
  }

  const visibleText = (transcriptText.textContent || "").trim();
  if (visibleText && visibleText !== "Your transcript will appear here...") {
    return visibleText;
  }

  return "";
}

function downloadTextFile(content, filePrefix) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const link = document.createElement("a");
  link.href = url;
  link.download = `${filePrefix}-${stamp}.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function floatTo16BitPCM(float32Array) {
  const output = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, float32Array[i]));
    output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output;
}

function downmixToMono(inputBuffer) {
  const channelCount = inputBuffer.numberOfChannels;
  const frameCount = inputBuffer.length;

  if (channelCount <= 1) {
    return inputBuffer.getChannelData(0);
  }

  const mono = new Float32Array(frameCount);
  for (let channel = 0; channel < channelCount; channel += 1) {
    const data = inputBuffer.getChannelData(channel);
    for (let i = 0; i < frameCount; i += 1) {
      mono[i] += data[i] / channelCount;
    }
  }
  return mono;
}

function getWsUrl(sampleRate) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/ws/transcribe?sample_rate=${sampleRate}`;
}

function hasLegacyGetUserMedia() {
  return Boolean(
    navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia || navigator.msGetUserMedia
  );
}

function getMicAccessHint() {
  if (!window.isSecureContext) {
    return "This page is using HTTP on a public IP. Browsers require HTTPS (or localhost) for microphone access.";
  }

  return "Your browser does not expose the microphone API in this context.";
}

function requestUserMedia(constraints) {
  if (navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function") {
    return navigator.mediaDevices.getUserMedia(constraints);
  }

  const legacyGetUserMedia =
    navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia || navigator.msGetUserMedia;

  if (!legacyGetUserMedia) {
    return Promise.reject(new Error(getMicAccessHint()));
  }

  return new Promise((resolve, reject) => {
    legacyGetUserMedia.call(navigator, constraints, resolve, reject);
  });
}

function cleanupAudioNodes() {
  if (processorNode) {
    processorNode.disconnect();
    processorNode.onaudioprocess = null;
    processorNode = null;
  }
  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  }
}

function clearStopTimeout() {
  if (stopTimeoutId) {
    window.clearTimeout(stopTimeoutId);
    stopTimeoutId = null;
  }
}

function closeSocket() {
  if (!ws) {
    return;
  }
  try {
    ws.close();
  } catch (_) {
    // Ignore close failures during teardown.
  }
  ws = null;
}

function waitForSocketOpen(socket, timeoutMs = 6000) {
  return new Promise((resolve, reject) => {
    if (socket.readyState === WebSocket.OPEN) {
      resolve();
      return;
    }

    const timeout = window.setTimeout(() => {
      reject(new Error("WebSocket connection timeout"));
    }, timeoutMs);

    socket.addEventListener(
      "open",
      () => {
        window.clearTimeout(timeout);
        resolve();
      },
      { once: true }
    );

    socket.addEventListener(
      "error",
      () => {
        window.clearTimeout(timeout);
        reject(new Error("WebSocket failed to open"));
      },
      { once: true }
    );
  });
}

async function waitForJobResult(jobId, timeoutMs = 180000, pollIntervalMs = 1000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Failed to fetch job status");
    }

    if (payload.status === "completed") {
      return payload.result || "";
    }
    if (payload.status === "failed") {
      throw new Error(payload.error || "Job failed");
    }

    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }

  throw new Error("Timed out waiting for async job completion");
}

async function startRecording() {
  summarySection.classList.add("hidden");
  summaryText.textContent = "Summary appears here after processing.";
  finalizedSegments = [];
  livePartial = "";
  transcriptOverride = "";
  rewritePresetSelect.value = "Rewrite this transcript to be concise and clear.";
  rewriteStatus.textContent = "Rewritten transcript will replace the live transcript text.";
  setRewriteDebug("reset for new recording");
  updateTranscriptView();

  try {
    if (!(navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function") && !hasLegacyGetUserMedia()) {
      throw new Error(`${getMicAccessHint()} Use HTTPS, or tunnel to localhost and open the app from http://localhost.`);
    }

    mediaStream = await requestUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    audioContext = new AudioContext();
    await audioContext.resume();
    const sampleRate = audioContext.sampleRate;

    ws = new WebSocket(getWsUrl(sampleRate));
    ws.binaryType = "arraybuffer";

    await waitForSocketOpen(ws);

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "partial") {
        transcriptOverride = "";
        livePartial = payload.text || "";
      }
      if (payload.type === "final") {
        transcriptOverride = "";
        if (payload.text) {
          finalizedSegments.push(payload.text);
        }
        livePartial = "";
      }
      if (payload.type === "session_complete") {
        setStatus("idle", "Finished");
        isStopping = false;
        clearStopTimeout();
        closeSocket();
        if (finalizedSegments.join(" ").trim()) {
          summarySection.classList.remove("hidden");
        }
      }
      if (payload.type === "error") {
        setStatus("idle", "Error");
        transcriptText.textContent = `Error: ${payload.message}`;
      }
      updateTranscriptView();
    };

    ws.onclose = () => {
      cleanupAudioNodes();
      if (isRecording || isStopping) {
        isRecording = false;
        isStopping = false;
        clearStopTimeout();
        startStopBtn.textContent = "Start Listening";
        setStatus("idle", "Disconnected");
      }
      ws = null;
    };

    ws.onerror = () => {
      isStopping = false;
      clearStopTimeout();
      setStatus("idle", "Error");
    };

    sourceNode = audioContext.createMediaStreamSource(mediaStream);
    processorNode = audioContext.createScriptProcessor(4096, 1, 1);

    processorNode.onaudioprocess = (event) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        return;
      }
      const monoData = downmixToMono(event.inputBuffer);
      const pcm16 = floatTo16BitPCM(monoData);
      // Send an isolated copy to avoid any chance of buffer reuse timing issues.
      const payload = pcm16.buffer.slice(0);
      ws.send(payload);
    };

    sourceNode.connect(processorNode);
    processorNode.connect(audioContext.destination);

    isRecording = true;
    startStopBtn.textContent = "Stop Listening";
    setStatus("recording", "Recording");
  } catch (error) {
    cleanupAudioNodes();
    closeSocket();
    isStopping = false;
    clearStopTimeout();
    setStatus("idle", "Error");
    transcriptText.textContent = `Microphone error: ${error.message}`;
  }
}

function stopRecording() {
  if (!isRecording) {
    return;
  }

  isRecording = false;
  isStopping = true;
  startStopBtn.textContent = "Start Listening";
  setStatus("processing", "Processing");
  cleanupAudioNodes();

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ event: "stop" }));
  } else {
    isStopping = false;
    setStatus("idle", "Finished");
  }

  clearStopTimeout();
  stopTimeoutId = window.setTimeout(() => {
    if (isStopping) {
      isStopping = false;
      setStatus("idle", "Finished");
      closeSocket();
    }
  }, 3000);
}

startStopBtn.addEventListener("click", async () => {
  if (isRecording) {
    stopRecording();
  } else {
    await startRecording();
  }
});

summarizeBtn.addEventListener("click", async () => {
  const fullTranscript = getTranscriptForActions();
  if (!fullTranscript) {
    summaryText.textContent = "No transcript available to summarize.";
    return;
  }

  summarizeBtn.disabled = true;
  summaryText.textContent = "Summarizing...";

  try {
    const response = await fetch("/api/summarize", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text: fullTranscript }),
    });

    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || "Failed to summarize transcript");
    }
    // Backward compatibility: some deployments still return direct summary payloads.
    if (typeof result.summary === "string") {
      summaryText.textContent = result.summary || "Summary returned empty content.";
      return;
    }
    // Additional compatibility: nested result object may carry the summary.
    if (result.result && typeof result.result.summary === "string") {
      summaryText.textContent = result.result.summary || "Summary returned empty content.";
      return;
    }
    if (typeof result.result === "string" && result.status !== "queued") {
      summaryText.textContent = result.result || "Summary returned empty content.";
      return;
    }
    if (result.status === "completed") {
      summaryText.textContent = result.result || "Summary returned empty content.";
      return;
    }
    if (!result.job_id) {
      throw new Error("No summarize job id returned");
    }

    summaryText.textContent = "Summarizing (queued)...";
    const summary = await waitForJobResult(result.job_id);
    summaryText.textContent = summary || "Summary returned empty content.";
  } catch (error) {
    summaryText.textContent = `Summary error: ${error.message}`;
  } finally {
    summarizeBtn.disabled = false;
  }
});

rewriteBtn.addEventListener("click", async () => {
  const fullTranscript = getTranscriptForActions();
  const rewritePrompt = (rewritePresetSelect.value || "Rewrite this transcript to be concise and clear.").trim();
  setRewriteDebug(`clicked, prompt selected, transcript chars=${fullTranscript.length}`);

  if (!fullTranscript) {
    rewriteStatus.textContent = "No transcript available to rewrite.";
    setRewriteDebug("blocked: no transcript available");
    return;
  }
  rewriteBtn.disabled = true;
  rewriteStatus.textContent = "Rewriting transcript...";
  setRewriteDebug("sending POST /api/rewrite");

  try {
    const response = await fetch("/api/rewrite", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text: fullTranscript, prompt: rewritePrompt }),
    });

    setRewriteDebug(`response received: HTTP ${response.status}`);
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || "Failed to rewrite transcript");
    }
    // Backward compatibility: some deployments still return direct rewrite payloads.
    if (typeof result.rewritten_text === "string") {
      transcriptOverride = result.rewritten_text || "";
      livePartial = "";
      if (transcriptOverride.trim()) {
        finalizedSegments = [transcriptOverride.trim()];
      }
      updateTranscriptView();
      rewriteStatus.textContent = "Transcript rewritten and updated above.";
      setRewriteDebug(`inline legacy success: rewritten chars=${(transcriptOverride || "").length}`);
      return;
    }
    // Additional compatibility: nested result object may carry rewritten text.
    if (result.result && typeof result.result.rewritten_text === "string") {
      transcriptOverride = result.result.rewritten_text || "";
      livePartial = "";
      if (transcriptOverride.trim()) {
        finalizedSegments = [transcriptOverride.trim()];
      }
      updateTranscriptView();
      rewriteStatus.textContent = "Transcript rewritten and updated above.";
      setRewriteDebug(`inline nested success: rewritten chars=${(transcriptOverride || "").length}`);
      return;
    }
    if (typeof result.result === "string" && result.status !== "queued") {
      transcriptOverride = result.result || "";
      livePartial = "";
      if (transcriptOverride.trim()) {
        finalizedSegments = [transcriptOverride.trim()];
      }
      updateTranscriptView();
      rewriteStatus.textContent = "Transcript rewritten and updated above.";
      setRewriteDebug(`inline string success: rewritten chars=${(transcriptOverride || "").length}`);
      return;
    }
    if (result.status === "completed") {
      transcriptOverride = result.result || "";
      livePartial = "";
      if (transcriptOverride.trim()) {
        finalizedSegments = [transcriptOverride.trim()];
      }
      updateTranscriptView();
      rewriteStatus.textContent = "Transcript rewritten and updated above.";
      setRewriteDebug(`inline success: rewritten chars=${(transcriptOverride || "").length}`);
      return;
    }
    if (!result.job_id) {
      throw new Error("No rewrite job id returned");
    }

    rewriteStatus.textContent = "Rewriting transcript (queued)...";
    setRewriteDebug(`queued job_id=${result.job_id}`);
    const rewritten = await waitForJobResult(result.job_id);

    transcriptOverride = rewritten || "";
    livePartial = "";
    if (transcriptOverride.trim()) {
      finalizedSegments = [transcriptOverride.trim()];
    }
    updateTranscriptView();
    rewriteStatus.textContent = "Transcript rewritten and updated above.";
    setRewriteDebug(`success: rewritten chars=${(transcriptOverride || "").length}`);
  } catch (error) {
    rewriteStatus.textContent = `Rewrite error: ${error.message}`;
    setRewriteDebug(`error: ${error.message}`);
  } finally {
    rewriteBtn.disabled = false;
  }
});

downloadTranscriptBtn.addEventListener("click", () => {
  const transcript = getTranscriptForActions();
  if (!transcript) {
    setRewriteDebug("download transcript blocked: no content");
    return;
  }

  downloadTextFile(transcript, "transcript");
  setRewriteDebug(`transcript downloaded: chars=${transcript.length}`);
});

downloadSummaryBtn.addEventListener("click", () => {
  const summary = (summaryText.textContent || "").trim();
  if (!summary || summary === "Summary appears here after processing." || summary === "Summarizing...") {
    setRewriteDebug("download summary blocked: no content");
    return;
  }

  downloadTextFile(summary, "summary");
  setRewriteDebug(`summary downloaded: chars=${summary.length}`);
});

setStatus("idle", "Idle");
