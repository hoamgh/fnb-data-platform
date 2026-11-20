    """AF-02A: offline, synthetic Google admission tests; no crawler or publisher invocation."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fnb_job
from fnb_platform.overture import eligibility, silver


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


class GoogleHandoff(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/demo/airflow',prefix='af02a-')
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.silver_id='2026-10-21.0'
        self.candidate_id='a'*24
        self.silver_dir=self.folder/'silver'/'releases'/self.silver_id
        self.candidate_dir=self.folder/'eligibility'/'releases'/self.candidate_id
        self.silver_dir.mkdir(parents=True)
        self.candidate_dir.mkdir(parents=True)

        row={f.name:None for f in silver.CURRENT_STATE_SCHEMA}
        row.update(source='overture',source_id='ov-1',source_release=self.silver_id,
                   source_manifest_sha256='b'*64,name='Example Cafe',is_fnb=True,
                   gold_candidate=True,dq_valid=True)
        self.store_silver=row
        silver_artifacts={}
        for name,table in {
            'current_state.parquet':pa.Table.from_pylist([row],schema=silver.CURRENT_STATE_SCHEMA),
            'missing_or_out_of_scope.parquet':pa.Table.from_pylist([],schema=silver.MISSING_STATE_SCHEMA),
        }.items():
            path=self.silver_dir/name
            pq.write_table(table,path)
            silver_artifacts[name]={'sha256':digest(path),'rows':table.num_rows,'fields':table.schema.names}
        boundary=self.silver_dir/silver.BOUNDARY_ARTIFACT
        boundary.write_bytes(silver.WARD_PATH.read_bytes())
        write_json(self.silver_dir/'dq_metrics.json',{'status':'synthetic'})
        silver_manifest={'release_id':self.silver_id,'artifacts':silver_artifacts,
                         'source_manifest_sha256':'c'*64,'bronze_manifest_sha256':'b'*64,
                         'boundary_artifact':silver.BOUNDARY_ARTIFACT,
                         'ward_polygon_sha256':digest(boundary),'dq_metrics_sha256':digest(self.silver_dir/'dq_metrics.json'),
                         'metrics':{'status':'synthetic'}}
        write_json(self.silver_dir/'manifest.json',silver_manifest)

        self.candidate_row={'source':'overture','source_id':'ov-1','source_release':self.silver_id,
                            'source_manifest_sha256':'b'*64,'name':'Example Cafe','address':None,
                            'longitude':106.7,'latitude':10.7,'is_fnb_candidate':True,
                            'candidate_status':'PROVISIONAL','eligibility_policy_version':eligibility.POLICY}
        self.schema=pa.schema([(k, pa.bool_() if k=='is_fnb_candidate' else
                                pa.float64() if k in ('longitude','latitude') else pa.string())
                               for k in self.candidate_row])
        self.write_eligibility()

        crawl=self.folder/'crawl'
        crawl.mkdir()
        (crawl/'sample.json').write_text('{}',encoding='utf-8')
        self.conf={'mode':'replay','overture_release_id':self.silver_id,
                   'eligibility_run_id':self.candidate_id,
                   'inputs':{'overture_path':str(self.silver_dir/'current_state.parquet'),
                             'catalog':str(self.candidate_dir/'candidates.parquet'),
                             'crawl_root':str(crawl)},
                   'releases':[{'manifest':str(self.silver_dir/'manifest.json'),'release_id':self.silver_id},
                               {'manifest':str(self.candidate_dir/'manifest.json'),'release_id':self.candidate_id}],
                   'pins':{}}
        self.refresh_pins()
        self.output=self.folder/'fnb_google_crawl'/'test'

    def write_eligibility(self, *, silver_digest=None):
        artifacts={}
        for name,table in {
            'eligibility.parquet':pa.Table.from_pylist([self.candidate_row],schema=self.schema),
            'candidates.parquet':pa.Table.from_pylist([self.candidate_row],schema=self.schema),
            'missing_from_release.parquet':pa.Table.from_pylist([],schema=pa.schema([('source_id',pa.string())])),
        }.items():
            path=self.candidate_dir/name
            pq.write_table(table,path)
            artifacts[name]={'sha256':digest(path),'rows':table.num_rows,'schema':str(table.schema)}
        write_json(self.candidate_dir/'dq_metrics.json',{'counts':{'candidates':1}})
        artifacts['dq_metrics.json']={'sha256':digest(self.candidate_dir/'dq_metrics.json')}
        write_json(self.candidate_dir/'manifest.json',{
            'run_id':self.candidate_id,'identity_status':'PROVISIONAL_ONLY',
            'signature':{'source_release':self.silver_id,'policy':eligibility.POLICY,
                         'inputs_sha256':{'output/overture-silver-m241/releases/'+self.silver_id+'/current_state.parquet':
                                          silver_digest or digest(self.silver_dir/'current_state.parquet')}},
            'metrics':{'counts':{'candidates':1}},'artifacts':artifacts})

    def refresh_pins(self):
        files=[Path(self.conf['inputs'][name]) for name in ('overture_path','catalog')]
        files.extend(Path(r['manifest']) for r in self.conf['releases'])
        files.append(Path(self.conf['inputs']['crawl_root'])/'sample.json')
        self.conf['pins']={str(path):digest(path) for path in files}

    def admit(self):
        return fnb_job.admission(self.conf,self.output)

    def test_valid_pinned_replay_skips_overture_and_crawl(self):
        # A pointer may advance; only the explicit immutable release is consumed.
        write_json(self.folder/'silver'/'current.json',{'release_id':'different'})
        self.assertEqual(self.admit()['status'],'ADMITTED')
        self.assertEqual(fnb_job.execute(self.conf,'crawl',self.output)['status'],'OFFLINE_REPLAY')

    def test_wrong_catalog_artifact_rejected(self):
        self.conf['inputs']['catalog']=self.conf['inputs']['overture_path']
        with self.assertRaisesRegex(ValueError,'immutable releases'):
            self.admit()

    def test_wrong_silver_artifact_rejected(self):
        self.conf['inputs']['overture_path']=self.conf['inputs']['catalog']
        with self.assertRaisesRegex(ValueError,'immutable releases'):
            self.admit()

    def test_missing_release_rejected(self):
        (self.candidate_dir/'manifest.json').unlink()
        with self.assertRaisesRegex(ValueError,'Pinned input missing'):
            self.admit()

    def test_mismatched_silver_fingerprint_rejected(self):
        self.write_eligibility(silver_digest='0'*64)
        self.refresh_pins()
        with self.assertRaisesRegex(ValueError,'fingerprint lineage mismatch'):
            self.admit()

    def test_mismatched_release_id_rejected(self):
        self.conf['eligibility_run_id']='c'*24
        with self.assertRaisesRegex(ValueError,'immutable releases'):
            self.admit()

    def test_latest_alias_rejected(self):
        self.conf['overture_release_id']='latest'
        with self.assertRaisesRegex(ValueError,'Invalid explicit Overture Silver release ID'):
            self.admit()

    def test_source_rejected_even_with_resealed_manifest(self):
        self.candidate_row['source']='other'
        self.write_eligibility()
        self.refresh_pins()
        with self.assertRaisesRegex(ValueError,'wrong source'):
            self.admit()

    def test_schema_rejected_even_with_resealed_manifest(self):
        self.schema=pa.schema([(f.name,pa.string() if f.name=='is_fnb_candidate' else f.type) for f in self.schema])
        self.candidate_row['is_fnb_candidate']='true'
        self.write_eligibility()
        self.refresh_pins()
        with self.assertRaisesRegex(ValueError,'catalog schema'):
            self.admit()

    def test_changed_candidate_pin_rejected(self):
        (self.candidate_dir/'candidates.parquet').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'Pinned input missing/changed'):
            self.admit()


if __name__=='__main__':
    unittest.main()
