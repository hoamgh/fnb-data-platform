FROM python:3.12.8-slim-bookworm AS code
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY crawl_experiment ./crawl_experiment
COPY experiments ./experiments
COPY scripts ./scripts
COPY pipelines ./pipelines
COPY config ./config
COPY requirements ./requirements
COPY tests ./tests
COPY docker ./docker
RUN useradd --uid 10000 --create-home runner

FROM code AS pipeline
RUN python -m venv /opt/etl && /opt/etl/bin/pip install --no-cache-dir -e '.[download,dbt]' -r requirements/crawl-state.txt
ENV PATH=/opt/etl/bin:$PATH PYTHONPATH=/app/src:/app
USER runner
CMD ["python", "docker/runtime/smoke.py", "pipeline"]

FROM code AS dbt
RUN python -m venv /opt/dbt && /opt/dbt/bin/pip install --no-cache-dir -e '.[dbt]' pytest==9.1.1
ENV PATH=/opt/dbt/bin:$PATH PYTHONPATH=/app/src:/app DBT_SEND_ANONYMOUS_USAGE_STATS=false
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
USER runner
ENTRYPOINT ["python", "docker/runtime/dbt_entry.py"]
CMD ["--version"]

FROM pipeline AS crawler
USER root
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver && rm -rf /var/lib/apt/lists/*
RUN /opt/etl/bin/pip install --no-cache-dir seleniumbase==4.54.11 crawlee==1.10.2 playwright==1.42.0 psutil==7.2.2
USER runner
ENV CHROME_BIN=/usr/bin/chromium
CMD ["python", "docker/runtime/smoke.py", "browser"]

FROM apache/airflow:2.10.5-python3.12 AS airflow
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=pipeline /opt/etl /opt/etl
COPY --from=dbt /opt/dbt /opt/dbt
COPY --from=code --chown=airflow:root /app /app
COPY --chown=airflow:root docker/runtime/airflow_entry.py /opt/airflow/fnb_entry.py
USER airflow
# Publication validators run in Airflow; keep dbt itself in its isolated venv.
RUN pip install --no-cache-dir duckdb==1.5.5
ENV PYTHONPATH=/app/src:/app
ENTRYPOINT ["/usr/bin/dumb-init", "--", "python", "/opt/airflow/fnb_entry.py"]
CMD ["scheduler"]

# Optional scheduler extension for future authorized browser tasks. The default
# scheduler remains browser-free; Python packages remain in their own venv.
FROM airflow AS airflow-browser
USER root
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver && rm -rf /var/lib/apt/lists/*
COPY --from=crawler /opt/etl /opt/crawler
RUN /opt/crawler/bin/python -c "from pathlib import Path; files=Path('/opt/crawler/bin').iterdir(); [(p.write_bytes(p.read_bytes().replace(b'#!/opt/etl/bin/python',b'#!/opt/crawler/bin/python',1))) for p in files if p.is_file() and not p.is_symlink() and p.read_bytes().startswith(b'#!/opt/etl/bin/python')]"
USER airflow
ENV CHROME_BIN=/usr/bin/chromium
