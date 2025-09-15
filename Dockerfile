# Minimal Python image
FROM python:3.12-slim

# Set up work directory
WORKDIR /app

# Prevent .pyc files, ensure logs are unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies only if needed (kept minimal here)
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     ca-certificates && \
#     rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Copy and install Python deps first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Cloud Run provides $PORT; default to 8080 for local testing
ENV PORT=8080

# Start the FastAPI app via Uvicorn
# Assumes your FastAPI instance is named `app` inside main.py
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
