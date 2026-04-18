FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    zstd \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts /app/scripts
COPY models /app/models

ENV VOSK_MODEL_PATH=/app/models/vosk-model-en-us-0.22 \
    VOSK_MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip \
    LLM_API_MODE=ollama \
    LLM_API_BASE=http://ollama:11434 \
    LLM_MODEL=llama3.2 \
    APP_PORT=8765

RUN chmod +x /app/scripts/download_vosk_model.sh /app/scripts/start_all.sh && /app/scripts/download_vosk_model.sh

EXPOSE 8765

CMD ["/app/scripts/start_all.sh"]
