# Build context is the REPO ROOT (not ingest/), so this image can carry the shared
# schema registry. cloudbuild.yaml passes `-f ingest/Dockerfile .` for that reason.
FROM python:3.12-slim

WORKDIR /app

COPY ingest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The single source of truth for the daily_summary schema, shared with the daily
# job's image. Copied rather than imported because the two images are separate —
# which is exactly why the column list used to be hand-mirrored and able to drift.
COPY schema ./schema
COPY ingest/main.py .
# The per-nutrient reference knowledge base served by GET /nutrients. Loaded from
# the module's own directory, so it must sit next to main.py in the image.
COPY ingest/nutrient_info.json .

ENV PORT=8080
# 300s timeout: strongest-first model chain with per-model retries can legitimately
# run long on a slow/overloaded upstream, and response time isn't critical here.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 300 main:app
