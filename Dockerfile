FROM python:3.11-slim

# Chromium + chromedriver da mesma versão (vêm juntos do Debian)
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PORT=7860 PYTHONUNBUFFERED=1
EXPOSE 7860
# 1 processo (a fila e o cache ficam na memória dele) com várias threads
CMD gunicorn -w 1 --threads 16 --timeout 900 -b 0.0.0.0:${PORT} app:app
