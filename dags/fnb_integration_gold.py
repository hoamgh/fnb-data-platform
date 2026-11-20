"""Airflow DAG: fnb_integration_gold (manual, pinned inputs)."""
from fnb_orchestration import make_dag

dag = make_dag("fnb_integration_gold")
