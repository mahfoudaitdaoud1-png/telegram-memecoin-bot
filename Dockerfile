# ----- base -----
FROM python:3.11-slim

# System hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install runtime deps (build tools only if you need extras)
RUN pip install --no-cache-dir --upgrade pip

# Copy requirements first for layer caching
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app code
COPY . /app

# Make the startup script executable
RUN chmod +x /app/start.sh

# Cloud Run listens on $PORT (defaults to 8080 if not provided)
ENV PORT=8080

# (Optional) document the port
EXPOSE 8080

# Launch
CMD ["/app/start.sh"]
