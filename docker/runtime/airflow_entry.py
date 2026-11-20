"""Load mounted secrets at runtime; never emit connection strings/passwords."""
from pathlib import Path
import os,sys,subprocess,json
from urllib.parse import quote

def secret(name):
    return Path('/run/secrets/'+name).read_text().strip()

os.umask(0o002)
password=secret('airflow_password')
os.environ['AIRFLOW__DATABASE__SQL_ALCHEMY_CONN']='postgresql+psycopg2://fnb_airflow:'+quote(password,safe='')+'@postgres:5432/airflow'
os.environ['AIRFLOW__CORE__FERNET_KEY']=secret('fernet_key')
os.environ['AIRFLOW__WEBSERVER__SECRET_KEY']=secret('webserver_key')
if sys.argv[1:]==['init']:
    subprocess.run(['airflow','db','migrate'],check=True)
    result=subprocess.run(['airflow','users','list','--output','json'],capture_output=True,text=True)
    if result.returncode:raise SystemExit('Cannot inspect Airflow users')
    users=None
    for position,char in enumerate(result.stdout):
        if char != '[':continue
        try:
            candidate,_=json.JSONDecoder().raw_decode(result.stdout[position:])
            if isinstance(candidate,list) and all(isinstance(u,dict) and 'username' in u for u in candidate):
                users=candidate
                break
        except ValueError:pass
    if users is None:raise SystemExit('Cannot parse Airflow user inventory')
    if not any(u.get('username')=='demo' for u in users):
        cmd=['airflow','users','create','--username','demo','--firstname','FNB','--lastname','Demo',
             '--role','Admin','--email','demo@example.invalid','--password',secret('ui_password')]
        result=subprocess.run(cmd,capture_output=True,text=True)
        if result.returncode:raise SystemExit('Airflow user initialization failed; credentials suppressed')
    print('Airflow migration/user initialization complete')
else:
    os.execvp('airflow',['airflow',*sys.argv[1:]])
