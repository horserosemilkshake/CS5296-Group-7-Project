#!/usr/bin/env sh
set -eu

APP_PORT="${APP_PORT:-8765}"
WEB_CONCURRENCY="${WEB_CONCURRENCY:-2}"

echo "Starting FastAPI on port ${APP_PORT}"
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${APP_PORT}" --workers "${WEB_CONCURRENCY}"
