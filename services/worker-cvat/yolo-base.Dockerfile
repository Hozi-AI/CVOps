FROM python:3.9-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ultralytics pillow numpy
