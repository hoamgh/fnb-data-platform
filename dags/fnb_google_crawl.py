"""Airflow DAG: fnb_google_crawl (manual, pinned inputs)."""
from fnb_orchestration import make_dag

dag = make_dag("fnb_google_crawl")
