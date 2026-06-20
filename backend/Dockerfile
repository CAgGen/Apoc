# APoc backend — FastAPI + LangGraph + Crawl4AI grounding
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APOC_PORT=8800 \
    APOC_SKIP_SEARXNG_START=1 \
    APOC_SEARXNG_URL=http://searxng:8080 \
    APOC_DB_PATH=/data/apoc.db \
    APOC_RUNS_DIR=/data/runs

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first for layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Chromium + its OS libraries for Crawl4AI grounding (skip if you only ever use
# APOC_GROUNDING=anthropic_native, but bundled SearXNG grounding needs it)
RUN python -m playwright install --with-deps chromium

COPY . .

RUN mkdir -p /data/runs

EXPOSE 8800

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
