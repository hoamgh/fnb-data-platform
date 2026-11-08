"""Offline regression for DOM mode and the existing CDP enrichment wiring."""
import ast
from pathlib import Path

import pytest

from scripts import google_live_workers as workers


def test_dom_default_preserves_network_timestamp_wiring(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(workers, 'run', lambda *args, **kwargs: (captured.update(kwargs) or {"acceptance_pass": True}))
    workers.main(['--catalog', str(tmp_path / 'catalog.json'), '--output', str(tmp_path / 'out'), '--operator-submit'])
    assert captured['review_source'] == 'dom'
    with pytest.raises(SystemExit):
        workers.main(['--review-source', 'hybrid'])
    with pytest.raises(ValueError, match='Unsupported review source'):
        workers._run(tmp_path / 'missing', tmp_path / 'out', operator_submit=True, review_source='hybrid')
    assert not (tmp_path / 'out').exists()

    root = Path(__file__).resolve().parents[1]
    runner = ast.parse((root / 'crawl_experiment/runners/full_crawl_runner.py').read_text(encoding='utf-8'))
    calls = [n for n in ast.walk(runner) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'GoogleMapsCrawler']
    assert calls
    assert all(not any(k.arg == 'timestamp_enrichment' for k in call.keywords) for call in calls)
    source = (root / 'crawl_experiment/sources/google_maps/crawler.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    init = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    defaults = dict(zip([a.arg for a in init.args.kwonlyargs], init.args.kw_defaults))
    assert ast.literal_eval(defaults['timestamp_enrichment']) is True
    assert 'start_timestamp_collector(driver) if self.timestamp_enrichment else None' in source
    assert 'extractor.timestamp_collector = timestamp_collector' in source
    assert 'timestamp_collector.failed = True' in source
    collector = (root / 'crawl_experiment/sources/google_maps/review_timestamps.py').read_text(encoding='utf-8')
    assert 'return NetworkTimestampCollector(driver)' in collector
