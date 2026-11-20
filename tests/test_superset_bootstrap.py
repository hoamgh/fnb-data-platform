"""Import boundary regression: ORM/encryption initialization belongs to create_app."""

import importlib.util
from pathlib import Path
import sys
import types
import pytest


@pytest.fixture
def bootstrap(monkeypatch):
    superset = types.ModuleType("superset")
    superset.__path__ = []
    superset.db = object()
    superset.security_manager = object()
    app = types.ModuleType("superset.app")

    def create_app():
        raise AssertionError(
            "Importing the bootstrap must not initialize an app/database"
        )

    app.create_app = create_app
    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.__path__ = []
    engine = types.ModuleType("sqlalchemy.engine")
    engine.URL = object()
    for name, module in [
        ("superset", superset),
        ("superset.app", app),
        ("sqlalchemy", sqlalchemy),
        ("sqlalchemy.engine", engine),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
    # Model modules are deliberately unavailable until app initialization. The
    # original top-level ORM imports fail here, as they did in the real image.
    path = (
        Path(__file__).resolve().parents[1] / "docker/analytics/configure_superset.py"
    )
    spec = importlib.util.spec_from_file_location("fnb_bootstrap_import_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_import_is_safe_without_initialized_superset_app(bootstrap):
    module = bootstrap
    assert callable(module.configure)
    assert module.MARKER == "Managed by fnb analytics v1"


def test_setup_generates_missing_secrets_without_replacing_existing_values(tmp_path):
    import shutil
    import subprocess

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "init_analytics_secrets.py"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts" / script.name, script
    )
    args = [sys.executable, "-B", str(script)]
    first = subprocess.run(args, capture_output=True, text=True, check=True)
    target = tmp_path / ".auth/fnb"
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    assert len(before) == 6 and "admin_password" in before
    assert all(len(value) == 64 for value in before.values())
    second = subprocess.run(args, capture_output=True, text=True, check=True)
    assert before == {p.name: p.read_bytes() for p in target.iterdir()}
    assert all(
        value.decode() not in first.stdout + second.stdout for value in before.values()
    )


@pytest.fixture
def assets():
    from zipfile import ZipFile

    yaml = pytest.importorskip("yaml")
    with ZipFile(
        Path(__file__).resolve().parents[1] / "docker/analytics/market_overview.zip"
    ) as bundle:
        return {
            name: yaml.safe_load(bundle.read(name))
            for name in bundle.namelist()
            if name.endswith(".yaml")
        }


def test_native_bundle_has_one_dashboard_and_complete_dataset_references(assets):
    dashboards = [v for k, v in assets.items() if "/dashboards/" in k]
    charts = [v for k, v in assets.items() if "/charts/" in k]
    datasets = [v for k, v in assets.items() if "/datasets/" in k]
    assert len(dashboards) == 1 and len(charts) == 20 and len(datasets) == 6
    assert len({v["uuid"] for v in charts}) == len(charts)
    assert {v["dataset_uuid"] for v in charts} == {v["uuid"] for v in datasets}
    physical = [v for v in datasets if v["table_name"] != "map_catalog"]
    assert all(v["sql"] is None and v["schema"] == "bi" for v in physical)


def test_native_connection_has_no_embedded_password_or_write_access(assets):
    from urllib.parse import urlsplit

    databases = [v for k, v in assets.items() if "/databases/" in k]
    assert len(databases) == 1
    database = databases[0]
    uri = urlsplit(database["sqlalchemy_uri"])
    assert uri.password is None and uri.username == "fnb_analytics_readonly"
    assert not database["allow_dml"] and not database["expose_in_sqllab"]


def test_native_tabs_cover_each_selected_chart_once(assets, bootstrap):
    dashboard = next(v for k, v in assets.items() if "/dashboards/" in k)
    layout = dashboard["position"]
    tabset = next(
        layout[k] for k in layout["GRID_ID"]["children"] if layout[k]["type"] == "TABS"
    )
    tabs = tabset["children"]
    assert [layout[k]["meta"]["text"] for k in tabs] == [
        "Tổng quan",
        "Loại hình & địa điểm",
        "Reviews quan sát",
    ]
    ids = [value for key in tabs for value in bootstrap.chart_ids(layout, key)]
    assert len(ids) == len(set(ids)) == 20
    layout_names = {
        v["meta"]["sliceName"]
        for v in layout.values()
        if isinstance(v, dict) and v.get("type") == "CHART"
    }
    assert layout_names == {
        v["slice_name"] for k, v in assets.items() if "/charts/" in k
    }
    nodes = [
        v for v in layout.values() if isinstance(v, dict) and v.get("type") == "CHART"
    ]
    assert len(nodes) == 20
    assert {v["meta"]["uuid"] for v in nodes} == {
        v["uuid"] for k, v in assets.items() if "/charts/" in k
    }
    for node in layout.values():
        if not isinstance(node, dict):
            continue
        assert all(child in layout for child in node.get("children", []))


def test_map_uses_registered_metric_and_bounded_native_dataset(assets):
    import json

    chart = next(
        v
        for k, v in assets.items()
        if "/charts/" in k and v["viz_type"] == "deck_scatter"
    )
    params = chart["params"]
    if isinstance(params, str):
        params = json.loads(params)
    dataset = next(
        v
        for k, v in assets.items()
        if "/datasets/" in k and v["uuid"] == chart["dataset_uuid"]
    )
    metric = params["point_radius_fixed"]
    assert metric["type"] == "metric"
    assert metric["value"] in {m["metric_name"] for m in dataset["metrics"]}
    assert "n <= 10000" in dataset["sql"]
