"""Bounded synthetic admission/failure tests; never claim real-data E2E."""
import sys,json,hashlib,unittest,uuid,tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from airflow.operators.python import PythonOperator
from airflow.utils.task_group import TaskGroup
sys.path.insert(0,'/opt/airflow/dags')
import fnb_job
from fnb_orchestration import GRAPHS,GROUPS,make_dag,task,validate_stage_result


def synthetic_publication(base,stage):
    """Tiny synthetic releases exercise real checksum/pointer/manifest verifiers."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from fnb_platform.google import places_silver as silver
    from fnb_platform.integration import category_quality,matching_policy

    def publish(root,rid,signature,artifacts=(),kind=None):
        release=root/'releases'/rid
        release.mkdir(parents=True)
        specs={}
        for name in artifacts:
            path=release/name
            if name.endswith('.parquet'):
                table=pa.table({'fixture':pa.array([],type=pa.string())})
                pq.write_table(table,path)
                specs[name]=dict(sha256=fnb_job.sha(path),rows=0,
                                 logical_sha256=silver._digest([]),schema=str(table.schema))
            else:
                path.write_text('{}' if name.endswith('.json') else 'header\n')
                specs[name]=dict(sha256=fnb_job.sha(path))
        manifest=dict(release_id=rid,signature=signature,artifacts=specs)
        if kind:manifest['publication_kind']=kind
        (release/'manifest.json').write_text(json.dumps(manifest))
        (root/'current.json').write_text(json.dumps(dict(release_id=rid,
                                         manifest_sha256=fnb_job.sha(release/'manifest.json'))))
        return release

    upstreams=[]
    names=('comparison',) if stage=='entity_resolution' else ('eligibility','google','entity_resolution')
    for idx,name in enumerate(names):
        upstream=base/name
        publish(upstream,format(idx+1,'024x'),{'inputs_sha256':{}},())
        upstreams.append(upstream)
    pins={}
    for upstream in upstreams:
        pointer=upstream/'current.json'
        rid=json.loads(pointer.read_text())['release_id']
        manifest=upstream/'releases'/rid/'manifest.json'
        pins.update({str(pointer):fnb_job.sha(pointer),str(manifest):fnb_job.sha(manifest)})
    rule=(matching_policy.VERSION if stage=='entity_resolution' else category_quality.VERSION)
    release=publish(base/stage,'f'*24,dict(rule_version=rule,inputs_sha256=pins),
                    fnb_job.CRITICAL_PUBLICATION_ARTIFACTS[stage],
                    'CATEGORY_QUALITY_ONLY_NOT_GOLD' if stage=='category' else None)
    return release,upstreams

class Contracts(unittest.TestCase):
    def test_critical_publication_contract_valid_and_rejections(self):
        base=Path('/demo/airflow/_synthetic')
        base.mkdir(parents=True,exist_ok=True)
        for stage in ('entity_resolution','category'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory(prefix='af04a-',dir=base) as folder:
                release,upstreams=synthetic_publication(Path(folder),stage)
                active,manifest,pointer=fnb_job.verify_critical_publication(stage,release.parent.parent,upstreams)
                self.assertEqual(active,release)
                self.assertEqual(manifest['release_id'],pointer['release_id'])
                evidence=dict(stage=stage,airflow_run_id='synthetic',status='PUBLISHED',release=str(release),
                              release_id=manifest['release_id'],pointer=pointer,
                              manifest_sha256=fnb_job.sha(release/'manifest.json'))
                self.assertIs(validate_stage_result(stage,'synthetic',evidence,Path(folder),{}),evidence)
                with self.assertRaisesRegex(ValueError,'non-success status'):
                    validate_stage_result(stage,'synthetic',dict(evidence,status='FAILED'),Path(folder),{})
                with self.assertRaisesRegex(ValueError,'unexpected publication status'):
                    validate_stage_result(stage,'synthetic',dict(evidence,status='REUSED_VERIFIED'),Path(folder),{})
                reuse_conf={'reuse':{stage:str(release.parent.parent)}}
                reused=dict(evidence,status='REUSED_VERIFIED')
                self.assertIs(validate_stage_result(stage,'synthetic',reused,Path(folder),reuse_conf),reused)
                with self.assertRaisesRegex(ValueError,'publication evidence mismatch'):
                    validate_stage_result(stage,'synthetic',dict(evidence,manifest_sha256='0'*64),Path(folder),{})
                # Re-seal an incomplete manifest so pointer SHA alone would pass.
                missing=fnb_job.CRITICAL_PUBLICATION_ARTIFACTS[stage][0]
                reduced=dict(manifest,artifacts={k:v for k,v in manifest['artifacts'].items() if k!=missing})
                (release/'manifest.json').write_text(json.dumps(reduced))
                (release.parent.parent/'current.json').write_text(json.dumps(dict(release_id=release.name,
                    manifest_sha256=fnb_job.sha(release/'manifest.json'))))
                with self.assertRaisesRegex(ValueError,'missing mandatory artifact'):
                    fnb_job.verify_critical_publication(stage,release.parent.parent,upstreams)

    def test_critical_publication_rejects_corruption_and_lineage(self):
        base=Path('/demo/airflow/_synthetic')
        base.mkdir(parents=True,exist_ok=True)
        for stage in ('entity_resolution','category'):
            for mutation in ('missing_pointer','corrupt_artifact','mismatched_lineage'):
                with self.subTest(stage=stage,mutation=mutation),tempfile.TemporaryDirectory(prefix='af04a-',dir=base) as folder:
                    release,upstreams=synthetic_publication(Path(folder),stage)
                    if mutation=='missing_pointer':(release.parent.parent/'current.json').unlink()
                    elif mutation=='corrupt_artifact':
                        (release/fnb_job.CRITICAL_PUBLICATION_ARTIFACTS[stage][-1]).write_text('corrupt')
                    else:
                        (upstreams[0]/'current.json').write_text('{"release_id":"0"}')
                    with self.assertRaises((RuntimeError,ValueError,FileNotFoundError)):
                        fnb_job.verify_critical_publication(stage,release.parent.parent,upstreams)

    def test_stage_result_status_and_identity_fail_closed(self):
        base=dict(stage='admit',airflow_run_id='r1',status='ADMITTED')
        self.assertEqual(validate_stage_result('admit','r1',base,Path('/demo/airflow'),{}),base)
        for changed in (dict(status='FAILED'),dict(status='PUBLISHED'),dict(stage='category'),
                        dict(airflow_run_id='older')):
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'Stage result'):
                validate_stage_result('admit','r1',dict(base,**changed),Path('/demo/airflow'),{})

    def test_airflow_zero_exit_requires_result_and_publication(self):
        base=Path('/demo/airflow/fnb_integration_gold')
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='af04a-',dir=base) as folder:
            output=Path(folder)
            conf={'campaign_id':output.name}
            context=dict(dag=SimpleNamespace(dag_id='fnb_integration_gold'),
                         dag_run=SimpleNamespace(conf=conf),run_id='synthetic')
            result_file=output/'category.result.json'
            for status,reason in ((None,FileNotFoundError),('INVALID_JSON',json.JSONDecodeError),
                                  ('FAILED',ValueError),('PUBLISHED',ValueError)):
                with self.subTest(status=status):
                    def launched(*args,**kwargs):
                        if status=='INVALID_JSON':
                            result_file.write_text('{invalid')
                        elif status is not None:
                            result_file.write_text(json.dumps(dict(stage='category',
                                airflow_run_id='synthetic',status=status)))
                        class Finished:
                            returncode=0
                            def communicate(self,timeout):return ('mock exit 0',None)
                        return Finished()
                    with patch('fnb_orchestration.subprocess.Popen',side_effect=launched):
                        with self.assertRaises(reason):
                            task('category',**context)

    def test_graphs_and_retries(self):
        for name,stages in GRAPHS.items():
            dag=make_dag(name)
            self.assertIsNone(dag.schedule_interval)
            self.assertEqual(dag.max_active_runs,1)
            self.assertEqual(dag.task_ids,stages)
            for left,right in zip(stages,stages[1:]):self.assertEqual(dag.get_task(left).downstream_task_ids,{right})
            self.assertEqual(dag.get_task('admit').retries,0)
        self.assertEqual(make_dag('fnb_integration_gold').get_task('gold').retries,1)
        stages=GRAPHS['fnb_integration_gold']
        self.assertEqual(stages.count('entity_resolution'),1)
        self.assertFalse({'er_v1','er_v2','er_v3'} & set(stages))
        self.assertEqual(stages[1:4],['comparison','entity_resolution','category'])
    def test_taskgroups_preserve_stage_execution_contract(self):
        expected={
            'fnb_overture_ingestion':[
                ('admission',('admit',)),
                ('ingestion',('acquire','comparison_source','bronze')),
                ('silver_transformation',('silver',)),
                ('publication_reporting',('report',)),
            ],
            'fnb_google_crawl':[
                ('admission_state',('admit','state')),
                ('collection',('crawl',)),
                ('silver_transformation',('places','reviews')),
                ('publication_checkpoint',('checkpoint','report')),
            ],
            'fnb_integration_gold':[
                ('admission',('admit',)),
                ('canonical_resolution',('comparison','entity_resolution','category')),
                ('mapping_publication',('mapping',)),
                ('gold_integration',('gold',)),
                ('dbt_analytics',('dbt_verify','area')),
                ('reporting',('report',)),
            ],
        }
        self.assertEqual(set(GRAPHS),set(expected))
        for dag_id,layout in expected.items():
            dag=make_dag(dag_id)
            stages=GRAPHS[dag_id]
            self.assertEqual(len(dag.tasks),len(stages))
            self.assertEqual([name for _,names in layout for name in names],stages)
            self.assertEqual([(group_id,names) for group_id,_,names in GROUPS[dag_id]],layout)
            self.assertEqual(set(dag.task_group.children),{group_id for group_id,_ in layout})
            self.assertTrue(all(isinstance(group,TaskGroup) for group in dag.task_group.children.values()))
            self.assertEqual(dag.default_args['execution_timeout'],timedelta(minutes=60))
            self.assertEqual(dag.default_args['retry_delay'],timedelta(seconds=30))
            expected_group={name:group_id for group_id,names in layout for name in names}
            for index,name in enumerate(stages):
                operator=dag.get_task(name)
                self.assertIsInstance(operator,PythonOperator)
                self.assertIs(operator.python_callable,task)
                self.assertEqual(operator.op_kwargs,{'stage':name})
                self.assertEqual(operator.task_group.group_id,expected_group[name])
                self.assertFalse(operator.task_group.prefix_group_id)
                self.assertTrue(operator.task_group.tooltip)
                self.assertEqual(operator.execution_timeout,timedelta(minutes=60))
                self.assertEqual(operator.retries,0 if name in ('admit','crawl','acquire') else 1)
                self.assertEqual(operator.upstream_task_ids,set(stages[index-1:index]) if index else set())
                self.assertEqual(operator.downstream_task_ids,set(stages[index+1:index+2]))
    def test_no_unpinned_or_live_default(self):
        for conf in [{},{'mode':'live'},{'mode':'download'}]:
            with self.assertRaises(ValueError):fnb_job.admission(conf,Path('/demo/airflow/unused'))
    def test_changes_and_cli_failure_block(self):
        folder=Path('/demo/airflow/_synthetic')/uuid.uuid4().hex;folder.mkdir(parents=True)
        source=folder/'input';source.write_text('fixture')
        conf={'pins':{str(source):hashlib.sha256(source.read_bytes()).hexdigest()},'inputs':{'overture_path':str(source),'google_root':str(folder)}}
        fnb_job.admission(conf,folder)
        with patch.object(fnb_job.subprocess,'run',return_value=type('Result',(),{'returncode':17})()):
            with self.assertRaisesRegex(RuntimeError,'exit=17'):fnb_job.execute(conf,'comparison',folder)
        source.write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'):fnb_job.admission(conf,folder)
    def test_historical_windows_lineage_block(self):
        folder=Path('/demo/airflow/_synthetic')/uuid.uuid4().hex;folder.mkdir(parents=True)
        p=folder/'manifest.json';p.write_text(json.dumps({'signature':{'inputs_sha256':{'D:/historical/input':'hash'}}}))
        with self.assertRaisesRegex(ValueError,'not container-portable'):fnb_job.admission({'pins':{str(p):fnb_job.sha(p)}},folder)

    def test_critical_exit_zero_without_publication_blocks(self):
        folder=Path('/demo/airflow/_synthetic')/uuid.uuid4().hex;folder.mkdir(parents=True)
        source=folder/'input';source.write_text('fixture')
        conf={'pins':{str(source):fnb_job.sha(source)}}
        for name in ('eligibility_root','google_root'):
            root=folder/name;root.mkdir()
            marker=root/'input';marker.write_text('fixture')
            conf.setdefault('inputs',{})[name]=str(root)
            conf['pins'][str(marker)]=fnb_job.sha(marker)
        for stage in ('entity_resolution','category'):
            with self.subTest(stage=stage):
                with patch.object(fnb_job.subprocess,'run',return_value=type('Result',(),{'returncode':0})()) as run:
                    with self.assertRaisesRegex(RuntimeError,'without current.json'):
                        fnb_job.execute(conf,stage,folder)
                command=run.call_args.args[0]
                self.assertIn('fnb_platform.integration.'+('entity_resolution' if stage=='entity_resolution' else 'category_quality'),command)
                self.assertNotIn('--baseline-root',command)

    def test_legacy_er_reuse_keys_are_not_silently_ignored(self):
        for stage in ('er_v1','er_v2','er_v3'):
            with self.assertRaisesRegex(ValueError,'Legacy ER task reuse'):
                fnb_job.admission({'reuse':{stage:'/inputs/old'}},Path('/demo/airflow/unused'))


def synthetic_google_places(base):
    """Build a real, checksum-verifiable Places bundle with an isolated pinned source."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from fnb_platform.google import places_silver as places

    overture=base/'overture'/'releases'/'2026-10-21.0'/'current_state.parquet'
    overture.parent.mkdir(parents=True)
    overture.write_bytes(b'pinned overture source')
    signature={'policy':places.POLICY,'inputs_sha256':{str(overture):fnb_job.sha(overture)},
               'code_sha256':{},'pyarrow_version':pa.__version__,'excluded_runs':[]}
    rid=places._digest(signature)[:24]
    root=base/'places'
    release=root/'releases'/rid
    release.mkdir(parents=True)
    artifacts={}
    for name,schema in (('silver/google_places.parquet',places.PLACE_SCHEMA),
                        ('silver/source_links.parquet',places.LINK_SCHEMA)):
        path=release/name
        path.parent.mkdir(exist_ok=True)
        table=pa.Table.from_pylist([],schema=schema)
        pq.write_table(table,path)
        artifacts[name]=dict(sha256=fnb_job.sha(path),rows=0,
                             schema=str(table.schema),logical_sha256=places._digest([]))
    for name in ('reports/google_silver_dq_summary.json','reports/google_silver_dq_details.csv'):
        path=release/name
        path.parent.mkdir(exist_ok=True)
        path.write_text('{}' if name.endswith('.json') else 'column\n')
        artifacts[name]={'sha256':fnb_job.sha(path)}
    (release/'manifest.json').write_text(json.dumps(dict(release_id=rid,signature=signature,
                                                        artifacts=artifacts)))
    (root/'current.json').write_text(json.dumps(dict(release_id=rid,
                                  manifest_sha256=fnb_job.sha(release/'manifest.json'))))
    return root,release,overture


def synthetic_acquisition_catalog(base):
    """Publish a real empty canonical generation; keep all fixture pins local."""
    from fnb_platform.overture import catalog

    source=base/'new.parquet'
    source.write_bytes(b'synthetic source pin')
    ward=base/'ward.json'
    geometry={'type':'Polygon','coordinates':[[[106.0,10.0],[107.0,10.0],
                                            [107.0,11.0],[106.0,10.0]]]}
    ward.write_text(json.dumps({'geometry':geometry}))
    handle=base/'catalog.parquet'
    manifest=dict(overture_release='2026-10-21.0',source_mode='cached_snapshot_bbox_filter',
                  source_path=str(source),
                  boundary_sha256=hashlib.sha256(json.dumps(geometry,sort_keys=True).encode()).hexdigest())
    catalog.persist_catalog(catalog.PlaceCatalog([],{},dict(canonical_persisted=0)),handle,manifest)
    root=catalog.publication_root(handle)
    pointer=root/'current.json'
    rid=json.loads(pointer.read_text())['generation']
    immutable=root/'generations'/rid/'catalog.manifest.json'
    pins={str(path):fnb_job.sha(path) for path in (source,ward,pointer,immutable)}
    conf=dict(source_release='2026-10-21.0',inputs={'new_path':str(source),'ward_path':str(ward)},
              reuse={'acquire':str(root)},pins=pins,
              releases=[{'manifest':str(immutable),'release_id':rid}])
    return root,root/'generations'/rid,source,ward,conf


class OvertureGooglePublicationContracts(unittest.TestCase):
    BASE=Path('/demo/airflow/_synthetic')

    def test_acquisition_valid_reuse_and_lineage_fail_closed(self):
        self.BASE.mkdir(parents=True,exist_ok=True)
        for mutation in ('valid','missing_pointer','corrupt_artifact','corrupt_manifest','wrong_pointer',
                         'wrong_snapshot','missing_snapshot','wrong_boundary','wrong_declaration',
                         'unpinned_pointer','changed_pin','wrong_root'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory(prefix='af04b1-',dir=self.BASE) as directory:
                base=Path(directory)
                root,release,source,ward,conf=synthetic_acquisition_catalog(base)
                if mutation=='missing_pointer':(root/'current.json').unlink()
                if mutation=='corrupt_artifact':(release/'catalog.parquet').write_bytes(b'corrupt')
                if mutation=='corrupt_manifest':(release/'catalog.manifest.json').write_text('{}')
                if mutation=='wrong_pointer':
                    selected=json.loads((root/'current.json').read_text())
                    selected['generation']='0'*64
                    (root/'current.json').write_text(json.dumps(selected))
                if mutation=='wrong_snapshot':
                    replacement=base/'other.parquet';replacement.write_bytes(b'other')
                    conf['inputs']['new_path']=str(replacement)
                if mutation=='missing_snapshot':conf['inputs'].pop('new_path')
                if mutation=='wrong_boundary':
                    ward.write_text(json.dumps({'geometry':{'type':'Point','coordinates':[0,0]}}))
                if mutation=='wrong_declaration':conf['releases'][0]['release_id']='0'*64
                if mutation=='unpinned_pointer':conf['pins'].pop(str(root/'current.json'))
                if mutation=='changed_pin':source.write_bytes(b'changed after pin')
                if mutation=='wrong_root':conf['reuse']['acquire']=str(base)
                if mutation!='valid':
                    if mutation=='wrong_declaration':
                        with self.assertRaisesRegex(ValueError,'Release identity'):
                            fnb_job.admission(conf,base)
                        continue
                    if mutation in ('unpinned_pointer','changed_pin'):
                        with self.assertRaisesRegex(ValueError,'Unpinned active pointer|Pinned input missing/changed'):
                            fnb_job.admission(conf,base)
                        continue
                    with self.assertRaises((ValueError,RuntimeError,FileNotFoundError,KeyError)):
                        fnb_job.verify_overture_google_publication('acquire',base,conf)
                    continue
                active,rid,pointer,digest=fnb_job.verify_overture_google_publication('acquire',base,conf)
                self.assertEqual((active,rid,digest),(release,release.name,release.name))
                evidence=dict(stage='acquire',airflow_run_id='r1',status='REUSED_VERIFIED',
                              release=str(active),release_id=rid,pointer=pointer,manifest_sha256=digest)
                self.assertEqual(validate_stage_result('acquire','r1',evidence,base,conf),evidence)
                self.assertEqual(fnb_job.execute(conf,'acquire',base)['status'],'REUSED_VERIFIED')
                fresh=dict(conf,reuse={})
                self.assertEqual(fnb_job.verify_overture_google_publication('acquire',base,fresh)[0],active)
                self.assertEqual(validate_stage_result('acquire','r1',
                    dict(evidence,status='PUBLISHED'),base,fresh)['status'],'PUBLISHED')
                wrong=dict(conf,releases=[{'manifest':conf['releases'][0]['manifest'],
                                           'release_id':'0'*64}])
                with self.assertRaisesRegex(ValueError,'Release identity'):
                    fnb_job.admission(wrong,base)

    def test_places_valid_reuse_and_missing_corrupt_pointer_lineage(self):
        self.BASE.mkdir(parents=True,exist_ok=True)
        for mutation in ('valid','missing','corrupt','wrong_pointer','lineage'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory(prefix='af04b1-',dir=self.BASE) as directory:
                base=Path(directory)
                root,release,overture=synthetic_google_places(base)
                conf={'inputs':{'overture_path':str(overture)}}
                if mutation=='missing':(root/'current.json').unlink()
                if mutation=='corrupt':(release/'reports/google_silver_dq_summary.json').write_text('corrupt')
                if mutation=='wrong_pointer':
                    selected=json.loads((root/'current.json').read_text())
                    selected['release_id']='0'*24
                    (root/'current.json').write_text(json.dumps(selected))
                if mutation=='lineage':overture.write_bytes(b'changed pinned source')
                with patch('fnb_platform.overture.silver.verify_silver',return_value={'release_id':'2026-10-21.0'}):
                    if mutation!='valid':
                        with self.assertRaises((RuntimeError,ValueError,FileNotFoundError)):
                            fnb_job.verify_overture_google_publication('places',base,conf)
                        continue
                    active,rid,pointer,digest=fnb_job.verify_overture_google_publication('places',base,conf)
                    self.assertEqual((active,rid,digest),(release,release.name,fnb_job.sha(release/'manifest.json')))
                    evidence=dict(stage='places',airflow_run_id='r1',status='PUBLISHED',
                                  release=str(active),release_id=rid,pointer=pointer,manifest_sha256=digest)
                    self.assertEqual(validate_stage_result('places','r1',evidence,base,{}),evidence)
                    reuse={'reuse':{'places':str(root)}}
                    reused=dict(evidence,status='REUSED_VERIFIED')
                    self.assertEqual(validate_stage_result('places','r1',reused,base,reuse),reused)
                    with self.assertRaisesRegex(ValueError,'unexpected publication status'):
                        validate_stage_result('places','r1',evidence,base,reuse)
                    conf.update(reuse,pins={str(release/'manifest.json'):digest})
                    with patch.object(fnb_job,'admission',return_value={'status':'ADMITTED'}):
                        self.assertEqual(fnb_job.execute(conf,'places',base)['status'],'REUSED_VERIFIED')

    def test_cli_zero_exit_without_places_publication_fails(self):
        self.BASE.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='af04b1-',dir=self.BASE) as directory:
            base=Path(directory)
            overture=base/'source';overture.write_text('fixture')
            conf={'inputs':{'overture_path':str(overture),'crawl_root':str(base)}}
            with patch.object(fnb_job,'admission',return_value={'status':'ADMITTED'}),\
                 patch.object(fnb_job.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
                with self.assertRaisesRegex(RuntimeError,'without current.json'):
                    fnb_job.execute(conf,'places',base)

    def test_bronze_silver_and_reviews_lineage_dispatch(self):
        from fnb_platform.overture import comparison_publication, silver
        from fnb_platform.google import places_silver
        from fnb_platform.gold import staging
        self.BASE.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='af04b1-',dir=self.BASE) as directory:
            base=Path(directory)
            rid='2026-10-21.0'
            bronze=base/'bronze'/'releases'/rid;bronze.mkdir(parents=True)
            (bronze/'manifest.json').write_text('{}')
            (bronze.parent.parent/'current.json').write_text(json.dumps(dict(release_id=rid,
                                              manifest_sha256=fnb_job.sha(bronze/'manifest.json'))))
            bronze_manifest={'new_release':rid,'source_manifest_sha256':'source','scope':{'ward':'fixture'}}
            source_manifest={'input_sha256':{'polygon':'boundary'}}
            bronze_pointer={'manifest_sha256':fnb_job.sha(bronze/'manifest.json')}
            pin=(bronze,bronze_manifest,source_manifest,bronze_pointer)
            with patch.object(comparison_publication,'read_active',return_value=(bronze,bronze_manifest)),\
                 patch.object(silver,'pin_and_validate',return_value=pin) as pinned:
                self.assertEqual(fnb_job.verify_overture_google_publication('bronze',base,{})[1],rid)
                pinned.assert_called_once()
            selected_silver=dict(release_id=rid,bronze_manifest_sha256=bronze_pointer['manifest_sha256'],
                                 source_manifest_sha256='source',scope={'ward':'fixture'},ward_polygon_sha256='boundary')
            sroot=base/'silver'/'releases'/rid;sroot.mkdir(parents=True)
            (sroot/'manifest.json').write_text('{}')
            (sroot.parent.parent/'current.json').write_text(json.dumps(dict(release_id=rid,
                                             manifest_sha256=fnb_job.sha(sroot/'manifest.json'))))
            with patch.object(silver,'read_silver_active',return_value=(sroot,selected_silver)),\
                 patch.object(silver,'pin_and_validate',return_value=pin):
                self.assertEqual(fnb_job.verify_overture_google_publication('silver',base,{})[1],rid)
                with self.assertRaisesRegex(ValueError,'upstream lineage mismatch'):
                    self._invalid_silver_lineage(base,selected_silver)
            review_id='d'*24
            review=base/'reviews'/'releases'/review_id;review.mkdir(parents=True)
            (review/'manifest.json').write_text('{}')
            (review.parent.parent/'current.json').write_text(json.dumps(dict(release_id=review_id,
                                             manifest_sha256=fnb_job.sha(review/'manifest.json'))))
            places=base/'places'/'releases'/('a'*24);places.mkdir(parents=True)
            context=dict(reviews_release_id=review_id,google_places_silver_release=places.name)
            with patch.object(places_silver,'read_active',return_value=places),\
                 patch.object(staging,'pin_reviews',return_value=({}, {},context,None)):
                self.assertEqual(fnb_job.verify_overture_google_publication('reviews',base,{})[1],review_id)
                context['google_places_silver_release']='wrong'
                with self.assertRaisesRegex(ValueError,'upstream Places lineage mismatch'):
                    fnb_job.verify_overture_google_publication('reviews',base,{})

    @staticmethod
    def _invalid_silver_lineage(base,manifest):
        with patch('fnb_platform.overture.silver.read_silver_active',
                   return_value=(base/'silver'/'releases'/'2026-10-21.0',dict(manifest,bronze_manifest_sha256='bad'))):
            fnb_job.verify_overture_google_publication('silver',base,{})

def synthetic_integration_release(root,stage,signature,kind=None,summary=None):
    """A tiny, real checksummed release for focused orchestration contract tests."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from fnb_platform.google.places_silver import _digest
    rid=hashlib.sha256((str(root)+stage).encode()).hexdigest()[:24]
    release=root/'releases'/rid
    release.mkdir(parents=True,exist_ok=True)
    manifest=dict(release_id=rid,signature=signature,artifacts={})
    if kind:manifest.update(publication_kind=kind,production_gold=False)
    for name in fnb_job.INTEGRATION_ARTIFACTS[stage]:
        file=release/name;file.parent.mkdir(parents=True,exist_ok=True)
        spec={}
        if name.endswith('.parquet'):
            table=pa.table({'fixture':pa.array([],type=pa.string())})
            pq.write_table(table,file)
            spec=dict(rows=0,logical_sha256=_digest([]),schema=str(table.schema))
        elif name.endswith('run_results.json'):
            file.write_text(json.dumps(dict(results=[dict(unique_id='model.fixture',status='success'),
                                                     dict(unique_id='test.fixture',status='pass')],args={'vars':'{}'})))
        else:file.write_text(json.dumps(summary or {}) if name.endswith('.json') else 'sample\n')
        spec['sha256']=fnb_job.sha(file)
        manifest['artifacts'][name]=spec
    (release/'manifest.json').write_text(json.dumps(manifest))
    (root/'current.json').write_text(json.dumps(dict(release_id=rid,
                                           manifest_sha256=fnb_job.sha(release/'manifest.json'))))
    return release


class IntegrationGoldExecutionContracts(unittest.TestCase):
    BASE=Path('/demo/airflow/_synthetic')

    def setUp(self):
        self.BASE.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='af04b2-',dir=self.BASE)
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.output=self.base/'output';self.output.mkdir()
        self.anchor=self.base/'anchor.txt';self.anchor.write_text('pinned')
        self.code=self.base/'code.py';self.code.write_text('rule')
        self.inputs={str(self.anchor):fnb_job.sha(self.anchor)}
        self.codes={str(self.code):fnb_job.sha(self.code)}

    def publish(self,stage,signature=None,kind=None,summary=None,root=None):
        signature=signature or dict(inputs_sha256=self.inputs,code_sha256=self.codes)
        return synthetic_integration_release(root or self.output/stage,stage,signature,kind,summary)

    def test_comparison_verified_reuse_and_mutations(self):
        from fnb_platform.integration import comparison_features as comparison
        ov=self.base/'current_state.parquet';ov.write_bytes(b'overture')
        google_root=self.base/'google';google_root.mkdir()
        places=synthetic_integration_release(google_root,'shadow',dict(inputs_sha256=self.inputs,code_sha256=self.codes))
        for name in ('silver/google_places.parquet','silver/source_links.parquet'):
            path=places/name;path.parent.mkdir(exist_ok=True);path.write_bytes(b'google')
        keys=[ov,places/'silver/google_places.parquet',places/'silver/source_links.parquet',
              places/'manifest.json',google_root/'current.json']
        signature=dict(inputs_sha256={str(p):fnb_job.sha(p) for p in keys},code_sha256=self.codes,
                       normalization_version=comparison.NORMALIZATION_VERSION,
                       address_rule_version=comparison.ADDRESS_VERSION,
                       comparison_rule_version=comparison.PAIR_VERSION)
        release=self.publish('comparison',signature)
        conf={'inputs':{'overture_path':str(ov),'google_root':str(google_root)}}
        self.assertEqual(fnb_job.verify_integration_publication('comparison',self.output,conf)[0],release)
        reused_root=self.output/'comparison';reuse=dict(conf,reuse={'comparison':str(reused_root)})
        evidence=dict(stage='comparison',status='REUSED_VERIFIED',airflow_run_id='r',
                      release=str(release),release_id=release.name,
                      pointer=json.loads((reused_root/'current.json').read_text()),
                      manifest_sha256=fnb_job.sha(release/'manifest.json'))
        self.assertEqual(validate_stage_result('comparison','r',evidence,self.output,reuse),evidence)
        with patch.object(fnb_job,'admission',return_value={'status':'ADMITTED'}):
            with patch.object(fnb_job,'verify_integration_publication',wraps=fnb_job.verify_integration_publication):
                self.assertEqual(fnb_job.execute(dict(reuse,pins={str(release/'manifest.json'):fnb_job.sha(release/'manifest.json')}),
                                                 'comparison',self.output)['status'],'REUSED_VERIFIED')
        for changed in ('missing_artifact','wrong_lineage','wrong_pointer'):
            with self.subTest(changed=changed):
                if changed=='missing_artifact':
                    file=release/'reports/normalization_summary.json';old=file.read_bytes();file.unlink()
                elif changed=='wrong_lineage':
                    old=ov.read_bytes();ov.write_bytes(b'changed')
                else:
                    file=reused_root/'current.json';old=file.read_bytes();file.write_text('{}')
                with self.assertRaises((ValueError,RuntimeError,KeyError,FileNotFoundError)):
                    fnb_job.verify_integration_publication('comparison',self.output,conf)
                if changed=='wrong_lineage':ov.write_bytes(old)
                else:file.write_bytes(old)

    def test_shadow_rejects_stale_audit_and_wrong_scope(self):
        from fnb_platform.integration import er_auto_accept as auto
        er=self.publish('shadow',root=self.output/'entity_resolution')
        category=self.publish('shadow',root=self.output/'category')
        google=self.publish('shadow',root=self.base/'google')
        csv=self.base/'suggestions.csv';csv.write_text('label\n')
        audit=self.base/'audit_summary.json'
        report=dict(status='PROVISIONAL_AI_ONLY_NO_DECISIONS_IMPORTED',integrity_checks={'hashes':True},
                    releases=dict(er_release_id=er.name,category_release_id=category.name,
                                  google_silver_release_id=google.name),
                    input_hashes={str(csv):fnb_job.sha(csv)})
        audit.write_text(json.dumps(report))
        pins=dict(self.inputs)
        for release in (er,category):
            pins[str(release/'manifest.json')]=fnb_job.sha(release/'manifest.json')
            pins[str(release.parent.parent/'current.json')]=fnb_job.sha(release.parent.parent/'current.json')
        pins.update({str(csv):fnb_job.sha(csv),str(audit):fnb_job.sha(audit)})
        signature=dict(inputs_sha256=pins,code_sha256=self.codes,mode='SHADOW_ONLY',rule_version=auto.VERSION,
                       human_pointer_sha256=None)
        release=self.publish('shadow',signature,'SHADOW_ONLY_NOT_OFFICIAL_MAPPING')
        conf={'inputs':{'google_root':str(google.parent.parent),'ai_csv':str(csv),
                        'ai_audit':str(audit),'human_root':str(self.base/'human')}}
        self.assertEqual(fnb_job.verify_integration_publication('shadow',self.output,conf)[0],release)
        report['releases']['er_release_id']='outdated';audit.write_text(json.dumps(report))
        with self.assertRaises((ValueError,RuntimeError)):
            fnb_job.verify_integration_publication('shadow',self.output,conf)
        audit.write_text(json.dumps(report|{'releases':dict(er_release_id=er.name,category_release_id=category.name,
                                                             google_silver_release_id=google.name)}))
        (self.base/'human').mkdir();(self.base/'human/current.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'human history'):
            fnb_job.verify_integration_publication('shadow',self.output,conf)

    def test_dbt_results_require_real_models_and_tests(self):
        target=self.base/'results.json'
        rows=[dict(unique_id='model.test',status='success'),dict(unique_id='test.check',status='pass')]
        target.write_text(json.dumps(dict(results=rows)))
        self.assertEqual(fnb_job.verify_dbt_results(target)['tests'],1)
        for invalid in ([],[rows[0]],[rows[1]],[dict(rows[0],status='skipped'),rows[1]],
                        [rows[0],dict(rows[1],status='fail')]):
            with self.subTest(invalid=invalid):
                target.write_text(json.dumps(dict(results=invalid)))
                with self.assertRaises(ValueError):fnb_job.verify_dbt_results(target)

    def test_optional_area_skip_and_requested_area_rejected(self):
        with patch.object(fnb_job,'admission',return_value={'status':'ADMITTED'}):
            self.assertEqual(fnb_job.execute({'inputs':{}},'area',self.output)['status'],'SKIPPED')
        record=dict(stage='area',airflow_run_id='r',status='SKIPPED')
        self.assertEqual(validate_stage_result('area','r',record,self.output,{'inputs':{}}),record)
        with self.assertRaisesRegex(ValueError,'skipped despite'):
            validate_stage_result('area','r',record,self.output,{'inputs':{'reference_root':'required'}})
        with self.assertRaisesRegex(ValueError,'non-success'):
            validate_stage_result('area','r',dict(record,status='NOT_REQUESTED'),self.output,{'inputs':{}})
        with self.assertRaisesRegex(ValueError,'reference root'):
            validate_stage_result('area','r',dict(record,status='PUBLISHED'),self.output,{'inputs':{}})
        with self.assertRaisesRegex(ValueError,'requested publication'):
            validate_stage_result('area','r',record,self.output,{'inputs':{},'reuse':{'area':'/inputs/area'}})

    def test_area_uses_selected_reused_gold_root(self):
        gold_root='/inputs/pinned-gold-release'
        published=self.output/'area'/'releases'/'abcdef012345abcdef012345'
        manifest={'release_id':published.name}
        published.mkdir(parents=True)
        (published/'manifest.json').write_text(json.dumps(manifest))
        with patch.object(fnb_job,'admission',return_value={}), \
                patch.object(fnb_job.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run, \
                patch.object(fnb_job,'verify_integration_publication',return_value=(published,manifest,{'release_id':published.name})):
            result=fnb_job.execute({'inputs':{'reference_root':'/inputs/pinned-reference'},
                                    'reuse':{'gold':gold_root}},'area',self.output)
        self.assertEqual(result['status'],'PUBLISHED')
        argv=run.call_args.args[0]
        self.assertEqual(argv[argv.index('--catalog-root')+1],gold_root)

    def test_report_rejects_stale_missing_and_failed_stages(self):
        from fnb_orchestration import GRAPHS
        run='r-af04b2'
        conf={'_airflow_run_id':run,'inputs':{}}
        output=self.base/'fnb_integration_gold'/'campaign';output.mkdir(parents=True)
        records={}
        for stage in GRAPHS['fnb_integration_gold'][:-1]:
            status='SKIPPED' if stage=='area' else 'ADMITTED' if stage=='admit' else 'DBT_VERIFIED' if stage=='dbt_verify' else 'PUBLISHED'
            records[stage]=dict(stage=stage,status=status,airflow_run_id=run)
            (output/(stage+'.result.json')).write_text(json.dumps(records[stage]))
        with patch.object(fnb_job,'admission',return_value={}):
            self.assertEqual(len(fnb_job.execute(conf,'report',output)['stages']),8)
            (output/'area.result.json').write_text(json.dumps(dict(records['area'],status='PUBLISHED')))
            with self.assertRaisesRegex(ValueError,'invalid current-run'):
                fnb_job.execute(conf,'report',output)
            (output/'area.result.json').write_text(json.dumps(records['area']))
            (output/'gold.result.json').write_text(json.dumps(dict(records['gold'],airflow_run_id='old')))
            with self.assertRaisesRegex(ValueError,'stale'):
                fnb_job.execute(conf,'report',output)
            (output/'gold.result.json').unlink()
            with self.assertRaises(FileNotFoundError):fnb_job.execute(conf,'report',output)

    def test_zero_exit_without_integration_publication_fails(self):
        fake=SimpleNamespace(returncode=0)
        with patch.object(fnb_job,'admission',return_value={}),patch.object(fnb_job.subprocess,'run',return_value=fake):
            with self.assertRaisesRegex(RuntimeError,'without current.json'):
                fnb_job.execute({'inputs':{'overture_path':str(self.anchor),'google_root':str(self.base)}},
                                'comparison',self.output)


if __name__=='__main__':unittest.main()
