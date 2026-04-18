#!/usr/bin/env sh
set -eu

MODEL_URL="${VOSK_MODEL_URL:-https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip}"
MODEL_DIR="${VOSK_MODEL_PATH:-/app/models/vosk-model-en-us-0.22}"
ARCHIVE_PATH="/tmp/vosk-model.zip"

if [ -d "$MODEL_DIR" ]; then
  echo "Vosk model already exists at $MODEL_DIR"
  exit 0
fi

mkdir -p "$(dirname "$MODEL_DIR")"
echo "Downloading Vosk model from $MODEL_URL"
curl -L "$MODEL_URL" -o "$ARCHIVE_PATH"

TMP_EXTRACT_DIR="/tmp/vosk-model-extract"
rm -rf "$TMP_EXTRACT_DIR"
mkdir -p "$TMP_EXTRACT_DIR"
unzip -q "$ARCHIVE_PATH" -d "$TMP_EXTRACT_DIR"

EXTRACTED_DIR=$(find "$TMP_EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -z "$EXTRACTED_DIR" ]; then
  echo "No model directory found after extraction"
  exit 1
fi

mv "$EXTRACTED_DIR" "$MODEL_DIR"
rm -f "$ARCHIVE_PATH"
rm -rf "$TMP_EXTRACT_DIR"

echo "Model downloaded to $MODEL_DIR"
