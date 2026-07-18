FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared with the ingest image — see schema/registry.py.
COPY schema ./schema
COPY src ./src

# Cloud Run Job entry point — runs the daily fetch + Sheet upsert once and exits.
ENTRYPOINT ["python", "-m", "src.run_daily"]
