"""Airflow DAG: fnb_overture_ingestion (manual, pinned inputs)."""
from fnb_orchestration import make_dag

dag = make_dag("fnb_overture_ingestion")
