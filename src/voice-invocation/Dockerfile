FROM python:3.13-slim

# System dependencies for PyAudio
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        portaudio19-dev \
        libasound2-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY client.py .

ENTRYPOINT ["python", "client.py"]
