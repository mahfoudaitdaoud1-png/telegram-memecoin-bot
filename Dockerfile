FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories
RUN mkdir -p /tmp/telegram-bot

# Copy all Python application files
COPY main.py .
COPY session_wallet_manager.py .
COPY multiuser_commands.py .
COPY phantom_connect.py .

# Copy handles file to the expected location
COPY handles_partial.txt /tmp/telegram-bot/handles_partial.txt

# Set environment variable for PORT (Cloud Run will inject this)
ENV PORT=8080

# Use uvicorn to run the FastAPI app
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
