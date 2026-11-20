"""Small CLI orchestration adapter. Domain publishers own all business/DQ policy."""
import os,sys,json,hashlib,subprocess
from datetime import datetime, timezone
from pathlib import Path


def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def checked_path(value):
    p=Path(value).resolve()
    if not any(p.is_relative_to(Path(base)) for base in ('/inputs','/demo/airflow')):
        raise ValueError('Inputs must be mounted /inputs or an upstream /demo/airflow release')
    return p


CRITICAL_PUBLICATION_ARTIFACTS={
    'entity_resolution':('er_results.parquet','review_queue.csv','er_summary.json'),
    'category':('category_normalization.parquet','category_audit.parquet',
                'category_conflicts_for_review.csv','category_dq_summary.json'),
}
OVERTURE_GOOGLE_PUBLICATIONS=frozenset(('acquire','bronze','silver','places','reviews'))
INTEGRATION_PUBLICATIONS=frozenset(('comparison','mapping','shadow','active','gold','area'))
INTEGRATION_ARTIFACTS={
    'comparison':('comparison/overture_features.parquet','comparison/google_features.parquet',
                  'comparison/address_pair_evidence.parquet','reports/normalization_summary.json'),
    'mapping':('effective_identity_mapping.parquet','summary.json'),
    'shadow':('effective_identity_mapping.parquet','summary.json'),
    'active':('effective_identity_mapping.parquet','auto_accept_validation_sample.csv','summary.json'),
    'gold':('dim_store.parquet','store_source_mapping.parquet','source_accounting.parquet',
            'link_accounting.parquet','store_identity_registry.parquet','summary.json',
            'dbt-target/run_results.json','dbt-target/manifest.json'),
    'area':('ward_assignment.parquet','store_area_mapping.parquet','mart_fnb_by_area.parquet',
            'dq_summary.json','summary.json','dbt-target/run_results.json','dbt-target/manifest.json'),
}
GOOGLE_PLACES_ARTIFACTS=frozenset(('silver/google_places.parquet','silver/source_links.parquet',
    'reports/google_silver_dq_summary.json','reports/google_silver_dq_details.csv'))


def verify_critical_publication(stage,output_root,upstream_roots):
    """Fail closed on missing publication, corrupt artifacts, or changed input lineage."""
    from fnb_platform.google.places_silver import read_active,verify_release

    root=Path(output_root).resolve()
    pointer=root/'current.json'
    if not pointer.is_file():
        raise RuntimeError(stage+' CLI exited 0 without current.json')
    active=read_active(root)  # Validates pointer ID, manifest hash, and declared artifacts.
    if active is None:
        raise RuntimeError(stage+' publication has no active release')
    manifest=verify_release(active)
    selected=json.loads(pointer.read_text(encoding='utf-8'))
    rid=selected['release_id']
    if (active!=root/'releases'/rid or manifest.get('release_id')!=rid):
        raise ValueError(stage+' publication release identity mismatch')
    required=CRITICAL_PUBLICATION_ARTIFACTS[stage]
    artifacts=manifest.get('artifacts',{})
    for name in required:
        spec=artifacts.get(name)
        if not isinstance(spec,dict) or not isinstance(spec.get('sha256'),str):
            raise ValueError(stage+' publication missing mandatory artifact: '+name)
        if not (active/name).is_file() or sha(active/name)!=spec['sha256']:
            raise ValueError(stage+' publication mandatory artifact checksum mismatch: '+name)
    signature=manifest.get('signature',{})
    if stage=='entity_resolution':
        from fnb_platform.integration.matching_policy import VERSION
        if signature.get('rule_version')!=VERSION:
            raise ValueError('Canonical ER requires current policy')
    else:
        from fnb_platform.integration.category_quality import VERSION
        if signature.get('rule_version')!=VERSION or manifest.get('publication_kind')!='CATEGORY_QUALITY_ONLY_NOT_GOLD':
            raise ValueError('Category publication policy/kind mismatch')
    fingerprints=signature.get('inputs_sha256')
    if not isinstance(fingerprints,dict) or not fingerprints:
        raise ValueError(stage+' publication missing input lineage')
    for path,digest in fingerprints.items():
        if not isinstance(path,str) or not isinstance(digest,str) or sha(path)!=digest:
            raise ValueError(stage+' publication input lineage mismatch: '+str(path))
    # The manifest must reference the exact currently selected upstream releases.
    for upstream in upstream_roots:
        upstream=checked_path(upstream)
        upstream_pointer=upstream/'current.json'
        if not upstream_pointer.is_file():
            raise ValueError(stage+' publication missing upstream pointer: '+str(upstream_pointer))
        selected_upstream=json.loads(upstream_pointer.read_text(encoding='utf-8'))
        upstream_id=selected_upstream.get('release_id',selected_upstream.get('run_id'))
        if not isinstance(upstream_id,str) or not upstream_id:
            raise ValueError(stage+' publication invalid upstream release ID')
        upstream_manifest=upstream/'releases'/upstream_id/'manifest.json'
        if (selected_upstream.get('manifest_sha256')!=sha(upstream_manifest) or
                fingerprints.get(str(upstream_pointer))!=sha(upstream_pointer) or
                fingerprints.get(str(upstream_manifest))!=sha(upstream_manifest)):
            raise ValueError(stage+' publication upstream lineage mismatch: '+str(upstream))
    return active,manifest,selected


def verify_overture_google_publication(stage,output,conf):
    """Verify the selected immutable publication with its owning domain verifier."""
    from fnb_platform.overture import catalog, comparison_publication, silver
    from fnb_platform.google import places_silver
    from fnb_platform.publication import check_hashes

    inputs=conf.get('inputs',{})
    reuse=conf.get('reuse',{}).get(stage)
    root=checked_path(reuse) if reuse else output/stage
    source=checked_path(inputs['comparison_source']) if inputs.get('comparison_source') else output/'comparison_source'
    if stage=='acquire':
        # Catalog uses generations/<manifest-sha256>, not releases/<release_id>.
        if reuse:
            # reuse.acquire is the publication ROOT (.../catalog.parquet.catalog),
            # as it is for the other stages; load_catalog expects the logical handle.
            if not root.name.endswith('.parquet.catalog'):
                raise ValueError('Acquisition reuse must select a *.parquet.catalog publication root')
            handle=root.with_name(root.name.removesuffix('.catalog'))
            if catalog.publication_root(handle).resolve()!=root:
                raise ValueError('Invalid acquisition catalog reuse root')
        else:
            handle=output/'catalog.parquet'
            root=catalog.publication_root(handle)
        pointer=root/'current.json'
        if not pointer.is_file():raise RuntimeError('acquire CLI exited 0 without catalog current.json')
        selected=json.loads(pointer.read_text(encoding='utf-8'))
        rid=selected.get('generation')
        if (selected.get('publication_version')!=1 or not isinstance(rid,str) or
                len(rid)!=64 or any(ch not in '0123456789abcdef' for ch in rid) or
                selected.get('manifest_sha256')!=rid):
            raise ValueError('Invalid acquisition catalog generation pointer')
        release=root/'generations'/rid
        manifest_path=catalog.manifest_path(release/'catalog.parquet')
        if not manifest_path.is_file() or sha(manifest_path)!=rid:
            raise ValueError('Acquisition catalog manifest checksum mismatch')
        # load_catalog verifies the immutable Parquet schema, rows and artifact hash.
        catalog.load_catalog(handle)
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('overture_release')!=conf.get('source_release'):
            raise ValueError('Acquisition catalog source release mismatch')
        if conf.get('mode','replay')!='download':
            if not inputs.get('new_path'):
                raise ValueError('Acquisition catalog requires an explicit pinned snapshot path')
            if manifest.get('source_mode')!='cached_snapshot_bbox_filter' or manifest.get('source_path')!=str(checked_path(inputs['new_path'])):
                raise ValueError('Acquisition catalog input snapshot lineage mismatch')
        if inputs.get('ward_path'):
            ward=json.loads(checked_path(inputs['ward_path']).read_text(encoding='utf-8'))
            boundary_hash=hashlib.sha256(json.dumps(ward['geometry'],sort_keys=True).encode()).hexdigest()
            if manifest.get('boundary_sha256')!=boundary_hash:
                raise ValueError('Acquisition catalog ward boundary lineage mismatch')
        return release, rid, selected, rid

    pointer=root/'current.json'
    if not pointer.is_file():
        raise RuntimeError(stage+' CLI exited 0 without current.json')
    selected=json.loads(pointer.read_text(encoding='utf-8'))
    rid=selected.get('release_id')
    if not isinstance(rid,str) or not rid or rid in ('.','..') or '/' in rid or '\\' in rid:
        raise ValueError(stage+' invalid publication release ID')
    release=root/'releases'/rid
    manifest_path=release/'manifest.json'
    if not manifest_path.is_file() or sha(manifest_path)!=selected.get('manifest_sha256'):
        raise ValueError(stage+' publication pointer/manifest checksum mismatch')

    if stage=='bronze':
        active,manifest=comparison_publication.read_active(root)
        if active!=release or manifest.get('new_release')!=rid:
            raise ValueError('Bronze publication release identity mismatch')
        # Reuse the Silver publisher's full Bronze/source pin validator.
        silver.pin_and_validate(root,source)
    elif stage=='silver':
        active,manifest=silver.read_silver_active(root)
        if active!=release or manifest.get('release_id')!=rid:
            raise ValueError('Overture Silver publication release identity mismatch')
        bronze_root=checked_path(conf['reuse']['bronze']) if conf.get('reuse',{}).get('bronze') else output/'bronze'
        upstream,bronze_manifest,source_manifest,bronze_pointer=silver.pin_and_validate(bronze_root,source)
        if (manifest.get('bronze_manifest_sha256')!=bronze_pointer['manifest_sha256'] or
                manifest.get('source_manifest_sha256')!=bronze_manifest['source_manifest_sha256'] or
                manifest.get('release_id')!=bronze_manifest['new_release'] or
                manifest.get('scope')!=bronze_manifest['scope'] or
                manifest.get('ward_polygon_sha256')!=source_manifest['input_sha256'].get('polygon')):
            raise ValueError('Overture Silver upstream lineage mismatch')
    elif stage=='places':
        active=places_silver.read_active(root)
        if active!=release:
            raise ValueError('Google Places publication release identity mismatch')
        manifest=places_silver.verify_release(release)
        if (manifest.get('release_id')!=rid or rid!=places_silver._digest(manifest.get('signature',{}))[:24] or
                manifest.get('signature',{}).get('policy')!=places_silver.POLICY or
                set(manifest.get('artifacts',{}))!=GOOGLE_PLACES_ARTIFACTS):
            raise ValueError('Google Places publication contract mismatch')
        overture=checked_path(inputs['overture_path'])
        ov_manifest=silver.verify_silver(overture.parent)
        if overture!=overture.parent/'current_state.parquet' or ov_manifest.get('release_id')!=overture.parent.name:
            raise ValueError('Google Places Overture input release mismatch')
        hashes=manifest['signature'].get('inputs_sha256',{})
        if hashes.get(str(overture))!=sha(overture):
            raise ValueError('Google Places upstream Overture lineage mismatch')
        check_hashes(hashes)
    elif stage=='reviews':
        from fnb_platform.gold.staging import pin_reviews
        places_root=checked_path(conf['reuse']['places']) if conf.get('reuse',{}).get('places') else output/'places'
        places=places_silver.read_active(places_root)
        if places is None:raise ValueError('Reviews publication missing Google Places upstream')
        _,_,context,_=pin_reviews(root,places/'silver/google_places.parquet')
        if context['reviews_release_id']!=rid or context['google_places_silver_release']!=places.name:
            raise ValueError('Reviews publication upstream Places lineage mismatch')
    else:
        raise ValueError('Unsupported Overture/Google publication stage: '+stage)
    return release,rid,selected,sha(manifest_path)


def verify_dbt_results(path):
    """Require actual models and tests, without failed/skipped/empty dbt gates."""
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    rows=data.get('results')
    if not isinstance(rows,list) or not rows or any(not isinstance(r,dict) or
            r.get('status') not in ('success','pass') for r in rows):
        raise ValueError('dbt results missing, empty, failed or skipped')
    models=sum(str(r.get('unique_id','')).startswith('model.') and r['status']=='success' for r in rows)
    tests=sum(str(r.get('unique_id','')).startswith('test.') and r['status']=='pass' for r in rows)
    if not models or not tests:
        raise ValueError('dbt build must execute models and tests')
    return dict(models=models,tests=tests,results=len(rows))


def verify_integration_publication(stage,output,conf):
    """Check real immutable domain evidence for both new and reused publications."""
    from fnb_platform.google import places_silver as silver
    from fnb_platform.publication import check_hashes
    from fnb_platform.gold import staging

    if stage not in INTEGRATION_PUBLICATIONS:
        raise ValueError('Unknown Integration publication: '+stage)
    root=checked_path(conf['reuse'][stage]) if conf.get('reuse',{}).get(stage) else output/stage
    pointer=root/'current.json'
    if not pointer.is_file():raise RuntimeError(stage+' CLI exited 0 without current.json')
    active=silver.read_active(root)
    if active is None:raise ValueError(stage+' missing selected release')
    selected=json.loads(pointer.read_text(encoding='utf-8'))
    manifest=silver.verify_release(active)
    signature=manifest.get('signature',{})
    if (active!=root/'releases'/selected.get('release_id','') or
            manifest.get('release_id')!=active.name):
        raise ValueError(stage+' release identity mismatch')
    artifacts=manifest.get('artifacts',{})
    for name in INTEGRATION_ARTIFACTS[stage]:
        if name not in artifacts or not (active/name).is_file() or sha(active/name)!=artifacts[name].get('sha256'):
            raise ValueError(stage+' missing/corrupt mandatory artifact: '+name)
    if stage=='gold' and conf.get('reviews_release'):
        for name in ('review_attachment.parquet','unmatched_review_accounting.parquet'):
            if name not in artifacts or not (active/name).is_file():
                raise ValueError('Gold missing review conservation artifact: '+name)
    fingerprints=signature.get('inputs_sha256')
    codes=signature.get('code_sha256')
    if not isinstance(fingerprints,dict) or not fingerprints or not isinstance(codes,dict) or not codes:
        raise ValueError(stage+' publication missing input/code provenance')
    check_hashes({**fingerprints,**codes})
    def selected_upstream(name):
        upstream=checked_path(conf['reuse'][name]) if conf.get('reuse',{}).get(name) else output/name
        release=silver.read_active(upstream)
        if release is None:raise ValueError(stage+' missing '+name+' release')
        manifest_path=release/'manifest.json'
        if (fingerprints.get(str((upstream/'current.json').resolve()))!=sha(upstream/'current.json') or
                fingerprints.get(str(manifest_path.resolve()))!=sha(manifest_path)):
            raise ValueError(stage+' mismatched '+name+' upstream lineage')
        return release
    kind={'shadow':'SHADOW_ONLY_NOT_OFFICIAL_MAPPING',
          'active':'ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING',
          'mapping':'ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING',
          'gold':'CANONICAL_GOLD_STAGING_NOT_PRODUCTION',
          'area':'AREA_ENRICHMENT_GOLD_STAGING_NOT_PRODUCTION'}.get(stage)
    if kind and (manifest.get('publication_kind')!=kind or (stage in ('mapping','active','gold','area') and
            manifest.get('production_gold') is not False)):
        raise ValueError(stage+' publication kind/scope mismatch')
    if stage=='comparison':
        from fnb_platform.integration import comparison_features as comparison
        if any(signature.get(k)!=v for k,v in (
                ('normalization_version',comparison.NORMALIZATION_VERSION),
                ('address_rule_version',comparison.ADDRESS_VERSION),
                ('comparison_rule_version',comparison.PAIR_VERSION))):
            raise ValueError('Comparison rule version mismatch')
        ov=checked_path(conf['inputs']['overture_path'])
        google=checked_path(conf['inputs']['google_root'])
        places=silver.read_active(google)
        if places is None or any(fingerprints.get(str(p.resolve()))!=sha(p) for p in (
                ov,places/'silver/google_places.parquet',places/'silver/source_links.parquet',
                places/'manifest.json',google/'current.json')):
            raise ValueError('Comparison Overture/Google lineage mismatch')
    elif stage=='mapping':
        from fnb_platform.integration import mapping_publication
        er=selected_upstream('entity_resolution')
        category=selected_upstream('category')
        google=silver.read_active(checked_path(conf['inputs']['google_root']))
        if (signature.get('mapping_version')!=mapping_publication.VERSION or
                signature.get('rule_version')!=staging.ACTIVE_VERSION or
                signature.get('source_context')!={'er_release_id':er.name,
                    'category_release_id':category.name,'google_silver_release_id':google.name}):
            raise ValueError('Canonical mapping policy/upstream context mismatch')
        human=checked_path(conf['inputs']['human_root'])/'current.json'
        if (signature.get('human_root')!=str(human.parent) or
                'human_pointer_sha256' not in signature or
                (sha(human) if human.is_file() else None)!=signature['human_pointer_sha256']):
            raise ValueError('Mapping human state changed')
        data,_,_=mapping_publication.review.load(er.parent.parent,category.parent.parent)
        _,history=mapping_publication.human.read_history(human.parent)
        import pyarrow.parquet as pq
        expected,_=mapping_publication.evaluate(data,
            pq.read_table(data[4]['refs']['google_places']['path']).to_pylist(),history,dict(input_hashes=fingerprints,
                human_release=str(mapping_publication.human.read_history(human.parent)[0]) if human.is_file() else None,
                human_history_unchanged=True))
        if signature.get('catalog_scope'):
            from fnb_platform.integration import catalog_scope
            scope=signature['catalog_scope'];boundary=checked_path(scope['boundary_path'])
            if (scope.get('policy')!=catalog_scope.POLICY or
                    fingerprints.get(str(boundary))!=sha(boundary) or scope.get('boundary_sha256')!=sha(boundary)):
                raise ValueError('Mapping catalog scope boundary not pinned')
            expected=catalog_scope.apply(expected,
                pq.read_table(data[4]['refs']['google_places']['path']).to_pylist(),boundary)
        actual=pq.read_table(active/'effective_identity_mapping.parquet')
        if actual.to_pylist()!=expected.to_pylist():
            raise ValueError('Mapping differs from canonical evidence/current human decisions')
    elif stage=='shadow':
        from fnb_platform.integration import er_auto_accept as auto
        er=selected_upstream('entity_resolution')
        category=selected_upstream('category')
        if signature.get('mode')!='SHADOW_ONLY' or signature.get('rule_version')!=auto.VERSION:
            raise ValueError('Shadow mode/policy mismatch')
        audit=checked_path(conf['inputs']['ai_audit'])
        csv=checked_path(conf['inputs']['ai_csv'])
        report=json.loads(audit.read_text(encoding='utf-8'))
        google=silver.read_active(checked_path(conf['inputs']['google_root']))
        context={'er_release_id':er.name,'category_release_id':category.name,
                 'google_silver_release_id':google.name if google else None}
        if (report.get('status')!='PROVISIONAL_AI_ONLY_NO_DECISIONS_IMPORTED' or
                not report.get('integrity_checks') or not all(report['integrity_checks'].values()) or
                report.get('releases')!=context or
                report.get('input_hashes',{}).get(str(csv.resolve()))!=sha(csv) or
                fingerprints.get(str(audit.resolve()))!=sha(audit) or
                fingerprints.get(str(csv.resolve()))!=sha(csv)):
            raise ValueError('Shadow audit missing, stale or not provisional')
        human=checked_path(conf['inputs']['human_root'])/'current.json'
        if (sha(human) if human.is_file() else None)!=signature.get('human_pointer_sha256'):
            raise ValueError('Shadow human history changed since audit')
    elif stage=='active':
        verify_integration_publication('shadow',output,conf)
        shadow=selected_upstream('shadow')
        from fnb_platform.gold.staging import ACTIVE_VERSION
        if signature.get('rule_version')!=ACTIVE_VERSION or signature.get('source_shadow_release')!=shadow.name:
            raise ValueError('Activation shadow lineage/policy mismatch')
        human=Path(signature.get('human_root',''))/'current.json'
        expected=signature.get('human_pointer_sha256')
        if (not human.is_absolute() or 'human_pointer_sha256' not in signature or
                (sha(human) if human.is_file() else None)!=expected):
            raise ValueError('Human Decision state changed since activation')
        _,_,effective,guard=staging.pin_inputs(root,conf['reuse']['category'] if conf.get('reuse',{}).get('category') else output/'category')
        guard()
        summary=json.loads((active/'summary.json').read_text(encoding='utf-8'))
        if (summary.get('activation_scope')!='GOLD_STAGING_ONLY' or summary.get('production_gold') is not False or
                summary.get('source_shadow_release')!=shadow.name or
                any(row.get('shadow_only') or row.get('activation_scope')!='GOLD_STAGING_ONLY' for row in effective)):
            raise ValueError('Active mapping scope/evidence mismatch')
    elif stage=='gold':
        verify_integration_publication('mapping',output,conf)
        selected_upstream('mapping')
        selected_upstream('category')
        paths,_,effective,guard=staging.pin_inputs(
            conf.get('reuse',{}).get('mapping') or output/'mapping',
            conf.get('reuse',{}).get('category') or output/'category')
        guard()
        import pyarrow.parquet as pq
        registry=pq.read_table(active/'store_identity_registry.parquet')
        tables={name:pq.read_table(active/name) for name in (
            'dim_store.parquet','store_source_mapping.parquet','source_accounting.parquet','link_accounting.parquet')}
        google_ids={(r['google_id_kind'],r['google_identity_value']) for r in
                    pq.read_table(paths['google_path'],columns=['google_id_kind','google_identity_value']).to_pylist()}
        metrics=staging.validate_staging(tables,registry,effective,google_ids)
        if conf.get('reviews_release'):
            review_paths,_,context,review_guard=staging.pin_reviews(
                checked_path(conf['inputs']['reviews_root']),paths['google_path'],release_id=conf['reviews_release'])
            review_guard()
            context=dict(context,effective_mapping_release_id=Path(paths['effective_mapping_path']).parent.name,
                         effective_mapping_manifest_sha256=sha(Path(paths['effective_mapping_path']).parent/'manifest.json'))
            if signature.get('reviews_context')!=context:
                raise ValueError('Gold Reviews/active mapping release lineage mismatch')
            tables.update({name:pq.read_table(active/name) for name in
                           ('review_attachment.parquet','unmatched_review_accounting.parquet')})
            current=pq.read_table(review_paths['reviews_current_path']).to_pylist()
            metrics.update(staging.validate_review_attachment(tables,current,effective,registry,reviews_context=context))
        summary=json.loads((active/'summary.json').read_text(encoding='utf-8'))
        if summary.get('production_gold') is not False or any(summary.get(k)!=v for k,v in metrics.items()):
            raise ValueError('Gold mapping/review accounting or production scope mismatch')
        dbt=verify_dbt_results(active/'dbt-target/run_results.json')
        if any(summary.get('dbt_build',{}).get(k)!=dbt[k] for k in ('models','tests')):
            raise ValueError('Gold dbt model/test evidence mismatch')
    elif stage=='area':
        verify_integration_publication('gold',output,conf)
        selected_upstream('gold')
        from fnb_platform.gold import area_enrichment
        from fnb_platform.gold.area_enrichment import VERSION
        if signature.get('rule_version')!=VERSION:
            raise ValueError('Area policy mismatch')
        area_enrichment.pin_inputs(checked_path(conf['inputs']['reference_root']),
                                   conf.get('reuse',{}).get('gold') or output/'gold')
        summary=json.loads((active/'summary.json').read_text(encoding='utf-8'))
        if summary.get('production_gold') is not False:
            raise ValueError('Area production scope mismatch')
        verify_dbt_results(active/'dbt-target/run_results.json')
    return active,manifest,selected


def verify_google_candidate_handoff(conf):
    """Bind the crawler's Eligibility catalog to the same immutable Overture Silver input as Places."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from fnb_platform.overture import eligibility, silver

    pins=conf['pins']
    inputs=conf.get('inputs',{})
    declared=conf.get('releases',[])
    try:
        silver.bronze.release_key(conf.get('overture_release_id'))
    except ValueError as exc:
        raise ValueError('Invalid explicit Overture Silver release ID') from exc

    def selected(key,filename,id_key):
        rid=conf.get(id_key)
        if not isinstance(rid,str) or not rid:
            raise ValueError('Google handoff requires explicit '+id_key)
        if key not in inputs:
            raise ValueError('Google handoff requires '+key)
        path=checked_path(inputs[key])
        if (path.name!=filename or path.parent.name!=rid or
                path.parent.parent.name!='releases'):
            raise ValueError('Google handoff requires an immutable releases/<id>/'+filename+' for '+key)
        manifest=path.parent/'manifest.json'
        if not path.is_file() or not manifest.is_file():
            raise ValueError('Missing Google handoff release/artifact: '+key)
        if str(path) not in pins or str(manifest) not in pins:
            raise ValueError('Google handoff artifact and manifest must be pinned: '+key)
        if not any(r.get('manifest')==str(manifest) and r.get('release_id')==rid for r in declared):
            raise ValueError('Google handoff release identity not declared: '+key)
        return path,manifest,rid

    overture,overture_manifest,overture_id=selected('overture_path','current_state.parquet','overture_release_id')
    catalog,candidate_manifest,candidate_id=selected('catalog','candidates.parquet','eligibility_run_id')
    if (len(candidate_id)!=24 or any(c not in '0123456789abcdef' for c in candidate_id)):
        raise ValueError('Invalid Eligibility run ID')

    expected={'source':pa.string(),'source_id':pa.string(),'source_release':pa.string(),
              'source_manifest_sha256':pa.string(),'name':pa.string(),'address':pa.string(),
              'longitude':pa.float64(),'latitude':pa.float64(),'is_fnb_candidate':pa.bool_(),
              'candidate_status':pa.string(),'eligibility_policy_version':pa.string()}
    schema=pq.read_schema(catalog)
    if any(field not in schema.names or schema.field(field).type!=typ for field,typ in expected.items()):
        raise ValueError('Eligibility catalog schema is not the published F&B candidate contract')

    silver_release=silver.verify_silver(overture.parent)
    if silver_release.get('release_id')!=overture_id:
        raise ValueError('Overture Silver release ID mismatch')
    candidate_release=eligibility.verify_release(catalog.parent)
    signature=candidate_release.get('signature',{})
    if candidate_release.get('run_id')!=candidate_id or signature.get('source_release')!=overture_id:
        raise ValueError('Eligibility/Overture release ID mismatch')
    if signature.get('policy')!=eligibility.POLICY or candidate_release.get('identity_status')!='PROVISIONAL_ONLY':
        raise ValueError('Unexpected Eligibility source/policy')
    if candidate_release['artifacts']['candidates.parquet']['sha256']!=pins[str(catalog)]:
        raise ValueError('Eligibility candidate fingerprint mismatch')
    if silver_release['artifacts']['current_state.parquet']['sha256']!=pins[str(overture)]:
        raise ValueError('Overture Silver fingerprint mismatch')

    silver_suffix='/releases/'+overture_id+'/current_state.parquet'
    references=[digest for name,digest in signature.get('inputs_sha256',{}).items()
                if name.replace('\\','/').endswith(silver_suffix)]
    if len(references)!=1 or references[0]!=pins[str(overture)]:
        raise ValueError('Eligibility to Silver fingerprint lineage mismatch')

    rows=pq.read_table(catalog,columns=list(expected)).to_pylist()
    if not rows or any(row['source']!='overture' or row['source_release']!=overture_id or
                       row['is_fnb_candidate'] is not True or row['candidate_status']!='PROVISIONAL' or
                       row['eligibility_policy_version']!=signature['policy'] or
                       row['source_manifest_sha256']!=silver_release['bronze_manifest_sha256']
                       for row in rows):
        raise ValueError('Eligibility catalog has wrong source, release or candidate status')


def verify_integration_handoff(conf):
    """Preflight the exact published Silver/Eligibility inputs used downstream."""
    from fnb_platform.overture import eligibility, silver as overture_silver
    from fnb_platform.google import places_silver
    from fnb_platform.gold.staging import pin_reviews

    inputs, pins, declared = conf.get('inputs',{}), conf['pins'], conf.get('releases',[])

    def release_pin(directory,release_id):
        manifest=directory/'manifest.json'
        if not manifest.is_file():
            raise ValueError('Integration handoff missing release manifest: '+str(manifest))
        if str(manifest) not in pins or not any(
                r.get('manifest')==str(manifest) and r.get('release_id')==release_id
                for r in declared):
            raise ValueError('Integration handoff requires explicitly pinned release: '+str(manifest))

    def artifact_pin(path):
        if not path.is_file() or str(path) not in pins or sha(path)!=pins[str(path)]:
            raise ValueError('Integration handoff missing/unpinned artifact: '+str(path))

    for key in ('overture_path','google_root','reviews_root','eligibility_root'):
        if key not in inputs:
            raise ValueError('Integration handoff requires input: '+key)
    overture=checked_path(inputs['overture_path'])
    ov_id=conf.get('overture_release_id')
    if (not isinstance(ov_id,str) or not ov_id or overture.name!='current_state.parquet'
            or overture.parent.name!=ov_id or overture.parent.parent.name!='releases'):
        raise ValueError('Integration requires pinned Overture Silver releases/<id>/current_state.parquet')
    artifact_pin(overture)
    release_pin(overture.parent,ov_id)
    ov_manifest=overture_silver.verify_silver(overture.parent)
    if ov_manifest.get('release_id')!=ov_id or ov_manifest['artifacts']['current_state.parquet']['sha256']!=pins[str(overture)]:
        raise ValueError('Integration Overture Silver release/fingerprint mismatch')

    places_id=conf.get('google_places_release_id')
    if not isinstance(places_id,str) or not places_id:
        raise ValueError('Integration requires explicit google_places_release_id')
    google_root=checked_path(inputs['google_root'])
    places=places_silver.read_active(google_root)
    if places is None or places!=google_root/'releases'/places_id:
        raise ValueError('Integration Google Places pointer/release mismatch')
    release_pin(places,places_id)
    artifact_pin(google_root/'current.json')
    gm=places_silver.verify_release(places)
    if gm.get('release_id')!=places_id or gm['signature'].get('policy')!=places_silver.POLICY:
        raise ValueError('Integration Google Places publication mismatch')
    ov_refs=[(path,digest) for path,digest in gm['signature']['inputs_sha256'].items()
             if Path(path).name=='current_state.parquet']
    if ov_refs!=[(str(overture),pins[str(overture)])]:
        raise ValueError('Integration Overture to Google Places lineage mismatch')
    google_path=places/'silver/google_places.parquet'
    for name in ('silver/google_places.parquet','silver/source_links.parquet'):
        path=places/name
        artifact_pin(path)
        if gm['artifacts'][name]['sha256']!=pins[str(path)]:
            raise ValueError('Integration Google Places artifact fingerprint mismatch')

    eligibility_id=conf.get('eligibility_run_id')
    eligibility_root=checked_path(inputs['eligibility_root'])
    el,em=eligibility.read_active(eligibility_root)
    if (not isinstance(eligibility_id,str) or el.name!=eligibility_id):
        raise ValueError('Integration Eligibility pointer/release mismatch')
    release_pin(el,eligibility_id)
    artifact_pin(eligibility_root/'current.json')
    if em.get('run_id')!=eligibility_id or em['signature'].get('source_release')!=ov_id:
        raise ValueError('Integration Eligibility/Overture release mismatch')
    el_refs=[(path,digest) for path,digest in em['signature']['inputs_sha256'].items()
             if Path(path).name=='current_state.parquet']
    if len(el_refs)!=1 or el_refs[0][1]!=pins[str(overture)]:
        raise ValueError('Integration Eligibility/Overture fingerprint mismatch')

    reviews_id=conf.get('reviews_release')
    reviews_root=checked_path(inputs['reviews_root'])
    if not isinstance(reviews_id,str) or not reviews_id:
        raise ValueError('Integration requires explicit reviews_release')
    review_pointer=reviews_root/'current.json'
    artifact_pin(review_pointer)
    current=json.loads(review_pointer.read_text())
    if current.get('release_id')!=reviews_id:
        raise ValueError('Integration Reviews pointer/release mismatch')
    review_release=reviews_root/'releases'/reviews_id
    release_pin(review_release,reviews_id)
    if current.get('manifest_sha256')!=pins[str(review_release/'manifest.json')]:
        raise ValueError('Integration Reviews pointer/manifest mismatch')
    _,_,reviews_context,_=pin_reviews(reviews_root,google_path,release_id=reviews_id)
    if reviews_context['google_places_silver_release']!=places_id:
        raise ValueError('Integration Reviews/Places release mismatch')


def admission(conf,output):
    if {'er_v1','er_v2','er_v3'} & set(conf.get('reuse',{})):
        raise ValueError('Legacy ER task reuse keys unsupported; pin entity_resolution explicitly')
    mode=conf.get('mode','replay')
    family=output.parent.name
    allowed={'fnb_overture_ingestion':{'replay','download'},'fnb_google_crawl':{'replay','live'},'fnb_integration_gold':{'replay'}}
    if family in allowed and mode not in allowed[family]:raise ValueError('Mode unsupported for this DAG')
    if mode=='download' and conf.get('inputs',{}).get('comparison_source'):raise ValueError('Download must build comparison from the acquired snapshot')
    if mode not in ('replay','live','download'):raise ValueError('Unknown execution mode')
    if mode=='download' and (conf.get('download_authorized') is not True or os.environ.get('FNB_ALLOW_DOWNLOADS')!='1'):
        raise ValueError('External acquisition requires explicit config and deployment authorization')
    if mode=='live' and (conf.get('live_authorized') is not True or os.environ.get('FNB_ALLOW_LIVE_CRAWL')!='1'):
        raise ValueError('Live crawl requires explicit config and deployment authorization')
    if mode=='live':
        limits=conf.get('limits',{})
        for name,maximum in [('limit',4),('workers',2),('max_reviews',50),('max_scrolls',10),('per_store_seconds',180),('total_seconds',900)]:
            if type(limits.get(name)) is not int or not 0 < limits[name] <= maximum:raise ValueError('Invalid bounded live limit: '+name)
    pins=conf.get('pins',{})
    if not pins:raise ValueError('Explicit pinned input hashes required; no fixture fallback')
    for value,digest in pins.items():
        p=checked_path(value)
        if not p.is_file() or sha(p)!=digest:raise ValueError('Pinned input missing/changed: '+str(p))
        if p.name=='manifest.json':
            manifest=json.loads(p.read_text())
            inputs=manifest.get('signature',{}).get('inputs_sha256',{})
            if any('\\' in k or (len(k)>1 and k[1]==':') for k in inputs):
                raise ValueError('Historical host-native lineage is not container-portable; do not reseal/remap')
    for release in conf.get('releases',[]):
        p=checked_path(release['manifest']);m=json.loads(p.read_text())
        catalog_manifest=(p.name=='catalog.manifest.json' and p.parent.parent.name=='generations'
                          and p.parent.parent.parent.name.endswith('.parquet.catalog'))
        rid=p.parent.name if catalog_manifest else m.get('release_id',m.get('run_id'))
        if str(p) not in pins or rid!=release['release_id']:
            raise ValueError('Release identity is not authoritatively pinned')
    for value in list(conf.get('inputs',{}).values())+list(conf.get('reuse',{}).values()):
        root=checked_path(value)
        pointer=root/'current.json'
        if root.is_dir() and pointer.exists():
            if str(pointer) not in pins:raise ValueError('Unpinned active pointer: '+str(pointer))
            selected=json.loads(pointer.read_text())
            is_catalog=root.name.endswith('.parquet.catalog')
            rid=selected.get('generation') if is_catalog else selected.get('release_id',selected.get('run_id'))
            manifest=(root/'generations'/str(rid)/'catalog.manifest.json' if is_catalog
                      else root/'releases'/str(rid)/'manifest.json')
            if str(manifest) not in pins:raise ValueError('Selected active manifest not pinned')
            if not any(r['manifest']==str(manifest) and r['release_id']==rid for r in conf.get('releases',[])):
                raise ValueError('Explicit expected release identity required for active root')
    # Every declared input file/root must have pinned evidence (absent human roots
    # are admitted separately and rechecked by the existing activation policy).
    for key,value in conf.get('inputs',{}).items():
        if key=='human_root':continue
        p=checked_path(value)
        if p.is_file() and str(p) not in pins:raise ValueError('Unpinned input: '+key)
        if p.is_dir() and not any(Path(k).is_relative_to(p) for k in pins):raise ValueError('Unpinned root: '+key)
        if not p.exists():raise ValueError('Missing input: '+key)
    if family=='fnb_google_crawl':verify_google_candidate_handoff(conf)
    if family=='fnb_integration_gold':verify_integration_handoff(conf)
    return {'status':'ADMITTED','input_files':len(pins),'live_crawl':False}


def execute(conf,stage,output):
    inputs=conf.get('inputs',{})
    def inp(k):return str(checked_path(inputs[k]))
    def dest(k):return str(output/k)
    def module(name,*args):return ['-m','fnb_platform.'+name,*map(str,args)]
    if stage=='admit':return admission(conf,output)
    admission(conf,output)
    if stage=='comparison_source' and inputs.get('comparison_source'):
        return {'status':'PINNED_COMPARISON_SOURCE','source':inp('comparison_source')}
    root=lambda k: conf.get('reuse',{}).get(k) or dest(k)
    def critical_upstreams(name):
        if name=='entity_resolution':return (root('comparison'),)
        if name=='category':return (inp('eligibility_root'),inp('google_root'),root('entity_resolution'))
        return ()
    reuse=conf.get('reuse',{}).get(stage)
    if reuse:
        if stage in OVERTURE_GOOGLE_PUBLICATIONS:
            from fnb_platform.overture import catalog
            active,rid,pointer,manifest_hash=verify_overture_google_publication(stage,output,conf)
            # Admission already pins the selected root, pointer and manifest.
            manifest_path=(catalog.manifest_path(active/'catalog.parquet') if stage=='acquire'
                           else active/'manifest.json')
            if str(manifest_path) not in conf['pins']:
                raise ValueError('Reuse manifest must be pinned')
            return {'status':'REUSED_VERIFIED','release':str(active),'release_id':rid,
                    'pointer':pointer,'manifest_sha256':manifest_hash}
        if stage in INTEGRATION_PUBLICATIONS:
            active,manifest,pointer=verify_integration_publication(stage,output,conf)
            if str(active/'manifest.json') not in conf['pins']:
                raise ValueError('Reuse manifest must be pinned')
            return {'status':'REUSED_VERIFIED','release':str(active),'release_id':manifest['release_id'],
                    'pointer':pointer,'manifest_sha256':sha(active/'manifest.json')}
        from fnb_platform.google.places_silver import read_active,verify_release
        p=checked_path(reuse);active=read_active(p)
        if active is None:raise ValueError('Missing active reuse release')
        if str(active/'manifest.json') not in conf['pins']:raise ValueError('Reuse manifest must be pinned')
        manifest=verify_release(active)
        if stage in CRITICAL_PUBLICATION_ARTIFACTS:
            active,manifest,pointer=verify_critical_publication(stage,p,critical_upstreams(stage))
            return {'status':'REUSED_VERIFIED','release':str(active),'release_id':manifest['release_id'],
                    'pointer':pointer,'manifest_sha256':sha(active/'manifest.json')}
        return {'status':'REUSED_VERIFIED','release':str(active),'release_id':manifest['release_id']}
    def acquisition_command():
        common=module('overture.acquisition','--ward',inp('ward_path'),'--output',dest('catalog.parquet'),'--release',conf['source_release'])
        return common+(['--download','--bronze-dir',dest('bronze_input')] if conf.get('mode')=='download' else ['--source',inp('new_path'),'--source-metadata',inp('new_metadata')])
    commands={
      'acquire':acquisition_command,
      'comparison_source':lambda:module('overture.comparison_run','--old-path',inp('old_path'),'--old-metadata',inp('old_metadata'),'--new-path',dest('bronze_input/places.parquet') if conf.get('mode')=='download' else inp('new_path'),'--new-metadata',dest('bronze_input/source_metadata.json') if conf.get('mode')=='download' else inp('new_metadata'),'--ward-path',inp('ward_path'),'--destination',dest('comparison_source')),
      'bronze':lambda:module('overture.comparison_publication','--source',inputs.get('comparison_source') or dest('comparison_source'),'--root',dest('bronze')),
      'silver':lambda:module('overture.silver','--source',inputs.get('comparison_source') or dest('comparison_source'),'--bronze-publication',root('bronze'),'--silver-root',dest('silver')),
      'places':lambda:module('google.places_silver','--crawl-root',inp('crawl_root') if conf.get('mode','replay')=='replay' else dest('crawl'),'--overture-silver',inp('overture_path'),'--output-root',dest('places')),
      'reviews':lambda:module('google.reviews_silver','--google-silver-root',root('places'),'--output-root',dest('reviews')),
      'comparison':lambda:module('integration.comparison_features','--overture-silver',inp('overture_path'),'--google-silver-root',inp('google_root'),'--output-root',dest('comparison')),
      'entity_resolution':lambda:module('integration.entity_resolution','--comparison-root',root('comparison'),'--output-root',dest('entity_resolution')),
      'category':lambda:module('integration.category_quality','--eligibility-root',inp('eligibility_root'),'--google-root',inp('google_root'),'--er-root',root('entity_resolution'),'--output-root',dest('category')),
      'mapping':lambda:module('integration.mapping_publication','--er-root',root('entity_resolution'),'--category-root',root('category'),'--human-root',inp('human_root'),'--output-root',dest('mapping')),
      'gold':lambda:module('gold.staging','build','--active-root',root('mapping'),'--category-root',root('category'),'--reviews-root',inp('reviews_root'),'--reviews-release',conf['reviews_release'],'--output-root',dest('gold')),
      'area':lambda:module('gold.area_enrichment','--reference-root',inp('reference_root'),'--catalog-root',root('gold'),'--output-root',dest('area')),
    }
    if stage=='crawl':
        if conf.get('mode','replay')=='replay':return {'status':'OFFLINE_REPLAY','live_crawl':False,'crawl_root':inp('crawl_root')}
        if conf.get('live_authorized') is not True or os.environ.get('FNB_ALLOW_LIVE_CRAWL')!='1':raise ValueError('Live crawl requires operator conf AND deployment opt-in')
        limits=conf.get('limits',{})
        for name,maximum in [('limit',4),('workers',2),('max_reviews',50),('max_scrolls',10),('per_store_seconds',180),('total_seconds',900)]:
            if type(limits.get(name)) is not int or not 0 < limits[name] <= maximum:raise ValueError('Invalid bounded live limit: '+name)
        command=[sys.executable,'/app/scripts/google_live_workers.py','--catalog',inp('catalog'),'--output',dest('crawl'),'--operator-submit']
        for name,value in limits.items():
            if name not in ('limit','workers','max_reviews','max_scrolls','per_store_seconds','total_seconds'):raise ValueError('Unknown campaign limit')
            command += ['--'+name.replace('_','-'),str(value)]
        with (output/'crawl.log').open('a') as log:
            result=subprocess.run(command,cwd='/app',env=dict(os.environ,PYTHONPATH='/app/src:/app'),stdout=log,stderr=subprocess.STDOUT,timeout=1000)
        from scripts.evaluate_google_smoke import evaluate
        verdict=evaluate(output/'crawl')
        if result.returncode not in (0,1) or not verdict.get('bounded_smoke_pass'):raise ValueError('Bounded campaign/access/publication checks failed')
        return {'status':'BOUNDED_LIVE_CAPTURE','live_crawl':True,'verdict':verdict}

    if stage=='state':
        from crawl_experiment.storage.postgres_crawl_state import PostgresCrawlState
        from psycopg.conninfo import make_conninfo
        b=PostgresCrawlState(make_conninfo(host='postgres',dbname='fnb_platform',user='fnb_crawler',password=Path('/run/secrets/crawler_password').read_text().strip()))
        with b.connection() as con:con.execute('SELECT 1')
        return {'status':'READ','watermarks':{s:str(b.read(s)) for s in conf.get('store_ids',[])},'live_crawl':False}
    if stage=='checkpoint':
        if conf.get('mode','replay')=='replay':return {'status':'REPLAY_STATE_UNCHANGED','live_crawl':False}
        from psycopg.conninfo import make_conninfo
        from crawl_experiment.storage.postgres_crawl_state import PostgresCrawlState
        from crawl_experiment.storage.review_repository import ReviewRepository
        from crawl_experiment.storage.parquet_review_writer import ParquetReviewWriter
        backend=PostgresCrawlState(make_conninfo(host='postgres',dbname='fnb_platform',user='fnb_crawler',password=Path('/run/secrets/crawler_password').read_text().strip()))
        count=0
        for job in (output/'crawl').glob('worker-*/job-*'):
            data=json.loads((job/'input.json').read_text());store=data['store']['id']
            repository=ReviewRepository(job/'reviews.sqlite3',run_id=conf['_airflow_run_id'],parquet_writer=ParquetReviewWriter(job/'lake',conf['_airflow_run_id'],datetime.now(timezone.utc).date().isoformat()))
            try:backend.complete(repository,store,True);count+=1
            finally:repository.close()
        return {'status':'CHECKPOINTED','stores':count}
    if stage=='dbt_verify':
        from fnb_platform.google.places_silver import read_active
        release,_,_=verify_integration_publication('gold',output,conf)
        data=json.loads((release/'dbt-target/run_results.json').read_text());variables=data['args']['vars']
        if isinstance(variables,str):variables=json.loads(variables)
        if not isinstance(variables,dict):raise ValueError('Invalid pinned Gold dbt variables')
        variables['store_registry_path']=str(release/'store_identity_registry.parquet')
        env=dict(os.environ,FNB_DBT_DATABASE=str(output/'dbt-verify.duckdb'),DBT_SEND_ANONYMOUS_USAGE_STATS='false')
        args=['/opt/dbt/bin/dbt','build','--project-dir','/app/pipelines/gold_staging','--profiles-dir','/app/pipelines/gold_staging','--target-path',str(output/'dbt-verify-target'),'--log-path',str(output/'dbt-verify-logs'),'--vars',json.dumps(variables)]
        results_path=output/'dbt-verify-target/run_results.json'
        results_path.unlink(missing_ok=True)  # Old results cannot satisfy the current dbt invocation.
        with (output/'dbt_verify.log').open('a') as log:subprocess.run(args,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1200)
        metrics=verify_dbt_results(results_path)
        return {'status':'DBT_VERIFIED','gold_release_id':release.name,**metrics}
    if stage=='area' and not inputs.get('reference_root'):
        if reuse:raise ValueError('Area reuse requires an admitted reference root')
        return {'status':'SKIPPED','reason':'No admitted area reference release'}
    if stage=='report':
        graphs={
            'fnb_overture_ingestion':('admit','acquire','comparison_source','bronze','silver'),
            'fnb_google_crawl':('admit','state','crawl','places','reviews','checkpoint'),
            'fnb_integration_gold':('admit','comparison','entity_resolution','category','mapping','gold','dbt_verify','area'),
        }
        expected=graphs[output.parent.name]
        allowed={'admit':{'ADMITTED'},'state':{'READ'},'crawl':{'OFFLINE_REPLAY','BOUNDED_LIVE_CAPTURE'},
                 'comparison_source':{'PINNED_COMPARISON_SOURCE','PUBLISHED','REUSED_VERIFIED'},
                 'checkpoint':{'REPLAY_STATE_UNCHANGED','CHECKPOINTED'},'dbt_verify':{'DBT_VERIFIED'},
                 'area':{'SKIPPED','PUBLISHED','REUSED_VERIFIED'}}
        records={}
        for name in expected:
            record=json.loads((output/(name+'.result.json')).read_text(encoding='utf-8'))
            statuses=allowed.get(name,{'PUBLISHED','REUSED_VERIFIED'})
            if (record.get('stage')!=name or record.get('airflow_run_id')!=conf.get('_airflow_run_id') or
                    record.get('status') not in statuses or
                    (name=='area' and (record['status']=='SKIPPED')!=
                     (not bool(inputs.get('reference_root') or conf.get('reuse',{}).get('area')))) or
                    (not (name=='area' and record['status']=='SKIPPED') and
                     name in INTEGRATION_PUBLICATIONS|OVERTURE_GOOGLE_PUBLICATIONS|set(CRITICAL_PUBLICATION_ARTIFACTS)
                     and record['status']!=('REUSED_VERIFIED' if conf.get('reuse',{}).get(name) else 'PUBLISHED'))):
                raise ValueError('Missing, stale or invalid current-run result: '+name)
            records[name]=record
        return {'status':'COMPLETE','stages':records,'output_root':str(output),'live_crawl':conf.get('mode')=='live'}
    if stage not in commands:raise ValueError('Unknown stage')
    command=commands[stage]();env=dict(os.environ,PYTHONPATH='/app/src:/app',DBT_SEND_ANONYMOUS_USAGE_STATS='false')
    log=output/(stage+'.log')
    with log.open('a') as f:
        result=subprocess.run([sys.executable,*command],cwd='/app',env=env,stdout=f,stderr=subprocess.STDOUT,timeout=3300)
    if result.returncode:raise RuntimeError(stage+' CLI failed, exit='+str(result.returncode)+'; see '+str(log))
    value={'status':'PUBLISHED','command':[sys.executable,*command],'log':str(log)}
    pointer=output/stage/'current.json'
    if stage in OVERTURE_GOOGLE_PUBLICATIONS:
        release,rid,selected,manifest_hash=verify_overture_google_publication(stage,output,conf)
        value.update(pointer=selected,release=str(release),release_id=rid,
                     manifest_sha256=manifest_hash)
    if stage in CRITICAL_PUBLICATION_ARTIFACTS:
        release,manifest,selected=verify_critical_publication(stage,pointer.parent,critical_upstreams(stage))
        value.update(pointer=selected,release=str(release),release_id=manifest['release_id'],
                     manifest_sha256=sha(release/'manifest.json'))
    if stage in INTEGRATION_PUBLICATIONS:
        release,manifest,selected=verify_integration_publication(stage,output,conf)
        value.update(pointer=selected,release=str(release),release_id=manifest['release_id'],
                     manifest_sha256=sha(release/'manifest.json'))
    if pointer.exists() and stage not in OVERTURE_GOOGLE_PUBLICATIONS|INTEGRATION_PUBLICATIONS:
        value['pointer']=json.loads(pointer.read_text());rid=value['pointer'].get('release_id',value['pointer'].get('run_id'))
        release=pointer.parent/'releases'/rid;value['release']=str(release)
        value['manifest_sha256']=sha(release/'manifest.json')
        for name in ['summary.json','dq_metrics.json','review_dq_summary.json','er_summary.json']:
            if (release/name).exists():value['metrics']=json.loads((release/name).read_text());break
    return value


if __name__=='__main__':
    request=json.loads(Path(sys.argv[1]).read_text());output=Path(request['output']).resolve()
    if not output.is_relative_to(Path('/demo/airflow')):raise ValueError('Separate demo output required')
    output.mkdir(parents=True,exist_ok=True)
    request['conf']['_airflow_run_id']=request['run_id']
    value=execute(request['conf'],request['stage'],output)
    value.update(airflow_run_id=request['run_id'],stage=request['stage'])
    target=output/(request['stage']+'.result.json');temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2));temporary.replace(target)
    print(json.dumps(value))
