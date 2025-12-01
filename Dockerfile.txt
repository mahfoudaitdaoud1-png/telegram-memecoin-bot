FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY handles.partial.txt /tmp/telegram-bot/handles.partial.txt

# Create the directory structure
RUN mkdir -p /tmp/telegram-bot

CMD ["python", "main.py"]
