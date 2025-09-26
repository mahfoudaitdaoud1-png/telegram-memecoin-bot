FROM python:3.11-slim

WORKDIR /app

# (Optional but recommended) Prevent .pyc and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Start the FastAPI app on the port Cloud Run expects
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
