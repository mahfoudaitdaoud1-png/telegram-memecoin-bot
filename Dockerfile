# --- Minimal production image for the bot ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# System deps (curl for healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential curl && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY main.py ./

# Runtime dirs
RUN mkdir -p /tmp/telegram-bot && \
    adduser --disabled-password --gecos "" appuser && \
    chown -R appuser:appuser /tmp/telegram-bot /app
USER appuser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8080"]
