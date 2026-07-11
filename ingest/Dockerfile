FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT=8080
# 300s timeout: strongest-first model chain with per-model retries can legitimately
# run long on a slow/overloaded upstream, and response time isn't critical here.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 300 main:app
