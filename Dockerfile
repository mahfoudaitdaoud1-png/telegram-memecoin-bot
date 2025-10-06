FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY handles.partial.txt /tmp/telegram-bot/handles.partial.txt

# Create the directory structure
RUN mkdir -p /tmp/telegram-bot

# Run FastAPI directly with Uvicorn (Cloud Run expects port 8080)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
