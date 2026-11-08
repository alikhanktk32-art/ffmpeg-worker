FROM python:3.12-slim

# FFmpeg does the actual editing; the Python app only orchestrates it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Render (and most hosts) inject the port as $PORT; default to 8000 locally.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
