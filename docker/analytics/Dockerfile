FROM python:3.12.8-slim-bookworm AS warehouse
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/app/src:/app DBT_SEND_ANONYMOUS_USAGE_STATS=false
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY pipelines ./pipelines
COPY config ./config
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir '.[dbt]' psycopg2-binary==2.9.10 \
    && useradd --uid 50000 --create-home analytics
USER analytics

FROM apache/superset:6.0.0 AS superset
USER root
# Official lean image needs a database driver; do not change ETL/dbt dependencies.
RUN uv pip install --python /app/.venv/bin/python --no-cache psycopg2-binary==2.9.10
USER superset
