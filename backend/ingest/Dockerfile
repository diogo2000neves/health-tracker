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
# The deterministic insights core, its policy config, and the Gemini narrator —
# all loaded dynamically by main.py at runtime via importlib.
COPY ingest/insights.py .
COPY ingest/nutrient_policy.json .
COPY ingest/narrator.py .
# The Coach (v2): durable storage, the canonical food taxonomy, the deterministic
# food-pattern analysis, the card feed and the long-term memory. These import each
# other by name, which works because everything is flattened into /app.
COPY ingest/coach_store.py .
COPY ingest/food_taxonomy.py .
COPY ingest/food_patterns.py .
COPY ingest/coach_feed.py .
COPY ingest/coach_memory.py .
# The non-food half of the coach: the deterministic findings for sleep, training,
# body composition and digestion (with the thresholds that encode their judgement),
# and the cross-domain link engine. Both are pure stdlib and loaded by name.
COPY ingest/domain_findings.py .
COPY ingest/domain_policy.json .
COPY ingest/links.py .
# The memory tiers: the append-only archive of everything the coach has said, the
# event detector that reads the user's own meal notes, the budgeted retrieval that
# decides what a prompt gets to see, and the weekly/monthly/yearly rollups.
COPY ingest/coach_archive.py .
COPY ingest/coach_events.py .
COPY ingest/coach_recall.py .
COPY ingest/coach_reports.py .

ENV PORT=8080
# 300s timeout: strongest-first model chain with per-model retries can legitimately
# run long on a slow/overloaded upstream, and response time isn't critical here.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 300 main:app
