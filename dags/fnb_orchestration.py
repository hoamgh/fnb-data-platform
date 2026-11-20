"""Airflow-only task plumbing. Commands execute domain CLIs in isolated envs."""
import hashlib,json,os,re,signal,subprocess
from pathlib import Path
from datetime import timedelta
import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.task_group import TaskGroup

GRAPHS={
 'fnb_overture_ingestion':['admit','acquire','comparison_source','bronze','silver','report'],
 'fnb_google_crawl':['admit','state','crawl','places','reviews','checkpoint','report'],
 'fnb_integration_gold':['admit','comparison','entity_resolution','category','mapping','gold','dbt_verify','area','report'],
}

# Presentation groups only. Stages keep their historical task IDs and linear edges.
# The tooltips distinguish publication / audit gates from transformations.
GROUPS={
 'fnb_overture_ingestion':[
  ('admission','Validate pinned inputs',('admit',)),
  ('ingestion','Acquire source, prepare comparison input, publish Bronze',('acquire','comparison_source','bronze')),
  ('silver_transformation','Transform and publish Overture Silver',('silver',)),
  ('publication_reporting','Report Overture run and publication status',('report',)),
 ],
 'fnb_google_crawl':[
  ('admission_state','Validate pinned candidates and initialize crawl state',('admit','state')),
  ('collection','Collect Google data',('crawl',)),
  ('silver_transformation','Publish Google Places and Reviews Silver',('places','reviews')),
  ('publication_checkpoint','Persist checkpoint and report publication status',('checkpoint','report')),
 ],
 'fnb_integration_gold':[
  ('admission','Validate pinned upstream releases',('admit',)),
  ('canonical_resolution','Canonical V3 evidence and category quality',('comparison','entity_resolution','category')),
  ('mapping_publication','Publish accepted mappings; preserve unresolved and human decisions',('mapping',)),
  ('gold_integration','Canonical catalog, mappings and review conservation',('gold',)),
  ('dbt_analytics','Build and test analytics; optional area enrichment',('dbt_verify','area')),
  ('reporting','Report actual stage outcomes',('report',)),
 ],
}

SUCCESS_STATUSES={
 'admit':{'ADMITTED'},'state':{'READ'},
 'crawl':{'OFFLINE_REPLAY','BOUNDED_LIVE_CAPTURE'},
 'checkpoint':{'REPLAY_STATE_UNCHANGED','CHECKPOINTED'},
 'dbt_verify':{'DBT_VERIFIED'},'report':{'COMPLETE'},
 'comparison_source':{'PINNED_COMPARISON_SOURCE','PUBLISHED','REUSED_VERIFIED'},
 'area':{'SKIPPED','PUBLISHED','REUSED_VERIFIED'},
}
for _stage in ('acquire','bronze','silver','places','reviews','comparison',
               'entity_resolution','category','mapping','gold'):
    SUCCESS_STATUSES[_stage]={'PUBLISHED','REUSED_VERIFIED'}


def validate_stage_result(stage,run_id,result,output,conf):
    if (not isinstance(result,dict) or result.get('stage')!=stage or
            result.get('airflow_run_id')!=run_id):
        raise ValueError('Stage result missing/mismatched stage or Airflow run ID: '+stage)
    if result.get('status') not in SUCCESS_STATUSES[stage]:
        raise ValueError('Stage result has non-success status: '+stage)
    if stage=='area' and result['status']=='SKIPPED':
        if conf.get('inputs',{}).get('reference_root') or conf.get('reuse',{}).get('area'):
            raise ValueError('Area skipped despite requested publication')
        return result
    if stage=='area' and not conf.get('inputs',{}).get('reference_root'):
        raise ValueError('Area publication requires an admitted reference root')
    if stage=='dbt_verify' and (not isinstance(result.get('models'),int) or result['models']<1 or
            not isinstance(result.get('tests'),int) or result['tests']<1 or
            not isinstance(result.get('results'),int) or result['results']<result['models']+result['tests'] or
            not re.fullmatch('[0-9a-f]{24}',str(result.get('gold_release_id','')))):
        raise ValueError('dbt verify missing real model/test or Gold release evidence')
    if stage in ('entity_resolution','category','acquire','bronze','silver','places','reviews',
                 'comparison','mapping','gold','area'):
        expected='REUSED_VERIFIED' if conf.get('reuse',{}).get(stage) else 'PUBLISHED'
        if result['status']!=expected:
            raise ValueError('Stage result unexpected publication status: '+stage)
        catalog_stage=stage=='acquire'
        root=Path(conf.get('reuse',{}).get(stage) or
                  (output/'catalog.parquet.catalog' if catalog_stage else output/stage)).resolve()
        pointer=root/'current.json'
        if not pointer.is_file():
            raise ValueError('Stage result missing required publication pointer: '+stage)
        selected=json.loads(pointer.read_text(encoding='utf-8'))
        rid=selected.get('generation' if catalog_stage else 'release_id')
        identity_pattern=('[0-9a-f]{64}' if catalog_stage else
                          r'\d{4}-\d{2}-\d{2}\.\d+' if stage in ('bronze','silver') else '[0-9a-f]{24}')
        if not isinstance(rid,str) or not re.fullmatch(identity_pattern,rid):
            raise ValueError('Stage result invalid publication release ID: '+stage)
        release=root/('generations' if catalog_stage else 'releases')/rid
        manifest=release/('catalog.manifest.json' if catalog_stage else 'manifest.json')
        if (result.get('release')!=str(release) or result.get('release_id')!=rid or
                result.get('pointer')!=selected or not manifest.is_file() or
                result.get('manifest_sha256')!=hashlib.sha256(manifest.read_bytes()).hexdigest() or
                selected.get('manifest_sha256')!=result.get('manifest_sha256') or
                (catalog_stage and selected.get('publication_version')!=1)):
            raise ValueError('Stage result publication evidence mismatch: '+stage)
    return result


def task(stage,**context):
    conf=dict(context['dag_run'].conf or {})
    campaign=conf.get('campaign_id')
    if not isinstance(campaign,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',campaign):
        raise ValueError('Explicit safe campaign_id and pinned input configuration required')
    output=Path('/demo/airflow')/context['dag'].dag_id/campaign
    output.mkdir(parents=True,exist_ok=True)
    # Reusing one campaign with different configuration is never silently allowed.
    signature=json.dumps(conf,sort_keys=True).encode();marker=output/'configuration.sha256'
    digest=hashlib.sha256(signature).hexdigest()
    try:
        with marker.open('x') as f:f.write(digest)
    except FileExistsError:
        if marker.read_text()!=digest:raise ValueError('Campaign configuration changed; choose a new campaign_id')
    request=output/(stage+'.request.json');request.write_text(json.dumps(dict(conf=conf,stage=stage,output=str(output),run_id=context['run_id'])))
    result_file=output/(stage+'.result.json')
    result_file.unlink(missing_ok=True)  # A previous retry/run must never supply success evidence.
    python='/opt/crawler/bin/python' if stage=='crawl' and conf.get('mode')=='live' else '/opt/etl/bin/python'
    if not Path(python).exists():raise ValueError('Required isolated runtime absent; use the explicit browser scheduler override for live mode')
    env=dict(os.environ,PYTHONPATH='/app/src:/app',PYTHONDONTWRITEBYTECODE='1')
    process=subprocess.Popen([python,'/opt/airflow/dags/fnb_job.py',str(request)],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
    try:
        text,_=process.communicate(timeout=3500)
    except BaseException:
        os.killpg(process.pid,signal.SIGTERM)
        try:
            interrupted,_=process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);interrupted,_=process.communicate()
        (output/(stage+'.task.log')).write_text(interrupted or 'Task interrupted; inspect native stage log')
        raise
    (output/(stage+'.task.log')).write_text(text)
    if process.returncode:raise RuntimeError('Stage '+stage+' failed; retained log '+str(output/(stage+'.task.log'))+'; '+text[-1800:])
    result=validate_stage_result(stage,context['run_id'],json.loads(result_file.read_text()),output,conf)
    metadata={k:result[k] for k in ('stage','status','release','release_id','pointer','manifest_sha256','live_crawl') if k in result}
    metadata.update(airflow_run_id=context['run_id'],artifact=str(output/(stage+'.result.json')))
    print(json.dumps(metadata))
    return metadata


def make_dag(dag_id):
    dag=DAG(dag_id,schedule=None,start_date=pendulum.datetime(2026,1,1,tz='UTC'),catchup=False,max_active_runs=1,
            default_args={'retries':1,'retry_delay':timedelta(seconds=30),'execution_timeout':timedelta(minutes=60)},tags=['fnb','pinned','manual'])
    with dag:
        previous=None
        for group_id,tooltip,stages in GROUPS[dag_id]:
            with TaskGroup(group_id=group_id,tooltip=tooltip,prefix_group_id=False):
                for name in stages:
                    current=PythonOperator(task_id=name,python_callable=task,op_kwargs={'stage':name},retries=0 if name in ('admit','crawl','acquire') else 1)
                    if previous:previous >> current
                    previous=current
    return dag
