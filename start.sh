#!/bin/sh
set -e

# If Cloud Run doesn’t inject PORT, default to 8080
PORT="${PORT:-8080}"

# Exec so signals are forwarded correctly
exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
