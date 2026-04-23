#!/usr/bin/env sh
set -eu

MODEL_URL="https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip"
MODEL_NAME="vosk-model-en-us-0.22"
ARCHIVE_NAME="${MODEL_NAME}.zip"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(dirname "$SCRIPT_DIR")

MODELS_DIR="${REPO_ROOT}/models"
TMP_DIR="${REPO_ROOT}/.tmp-vosk-troubleshoot"
ARCHIVE_PATH="${TMP_DIR}/${ARCHIVE_NAME}"
EXTRACT_DIR="${TMP_DIR}/extract"
EXTRACTED_MODEL_DIR="${EXTRACT_DIR}/${MODEL_NAME}"

echo "Removing existing models directory (if present): ${MODELS_DIR}"
rm -rf "$MODELS_DIR"

echo "Preparing temporary workspace: ${TMP_DIR}"
rm -rf "$TMP_DIR"
mkdir -p "$EXTRACT_DIR"

echo "Downloading ${MODEL_NAME}"
curl -L "$MODEL_URL" -o "$ARCHIVE_PATH"

echo "Decompressing ${ARCHIVE_NAME}"
unzip -q "$ARCHIVE_PATH" -d "$EXTRACT_DIR"

if [ ! -d "$EXTRACTED_MODEL_DIR" ]; then
  echo "Error: expected extracted directory not found at ${EXTRACTED_MODEL_DIR}" >&2
  exit 1
fi

echo "Creating fresh models directory"
mkdir -p "$MODELS_DIR"

echo "Moving ${MODEL_NAME} into models"
mv "$EXTRACTED_MODEL_DIR" "$MODELS_DIR/"

echo "Cleaning up temporary files"
rm -rf "$TMP_DIR"

echo "Done. Model is now at ${MODELS_DIR}/${MODEL_NAME}"
