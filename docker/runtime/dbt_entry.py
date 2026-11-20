"""One embedded DuckDB database/target/log directory per invocation."""
import os,sys,uuid
from pathlib import Path
os.umask(0o002)
job=Path('/demo/jobs')/uuid.uuid4().hex
job.mkdir(parents=True,exist_ok=False)
os.environ.setdefault('FNB_DBT_DATABASE',str(job/'dbt.duckdb'))
os.environ.setdefault('DBT_TARGET_PATH',str(job/'target'))
os.environ.setdefault('DBT_LOG_PATH',str(job/'logs'))
os.execv('/opt/dbt/bin/dbt',['dbt',*sys.argv[1:]])
