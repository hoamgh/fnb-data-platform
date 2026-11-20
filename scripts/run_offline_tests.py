"""Explicit fixture profiles; no crawling or runtime release publication.

Long local runs: launch manually; progress.jsonl, pytest.log and summary.json
are retained under the printed results directory, including on failure.
"""
import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import socket
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
# Curated executable contracts, not directory discovery or live experiments.
OFFLINE_SUITES = (
    "tests/test_catalog.py", "tests/test_categories.py",
    "tests/test_prepare_catalog.py", "tests/test_osm.py",
    "tests/test_overture_snapshot.py", "tests/test_overture_eligibility.py",
    "tests/test_spatial_prededup.py", "tests/test_google_silver.py",
    "tests/test_google_reviews_silver.py", "tests/test_google_reliability.py",
    "tests/test_comparison_features.py", "tests/test_entity_resolution.py",
    "tests/test_entity_resolution_v2.py", "tests/test_entity_resolution_v3.py",
    "tests/test_category_quality.py", "tests/test_er_category_review_export.py",
    "tests/test_human_decisions.py", "tests/test_er_auto_accept.py",
    "tests/test_gold_staging.py", "tests/test_area_enrichment.py",
    "tests/test_google_address_enrichment.py", "tests/test_address_reference_pilot.py",
    "tests/test_google_candidate_ranking.py", "tests/test_google_poi_input.py",
)
# Include tests with transitive dbt fixture dependencies even when their own
# bodies only validate Python results. No test-file/schema edits are needed.
DBT_TESTS = tuple("tests/test_gold_staging.py::" + name for name in (
    "test_review_attachment_real_dbt_and_reuse",
    "test_empty_and_zero_accepted_reviews_real_dbt",
    "test_review_attachment_python_dq_rejects_corruption",
    "test_review_inputs_change_blocks_fresh_and_reuse",
    "test_review_conservation_and_mapping_dbt_reject_bad_outputs",
    "test_exact_overture_anchor_python_guard",
    "test_exact_overture_anchor_dbt_data_test",
    "test_zero_accepted_exact_anchor_real_dbt",
    "test_real_dbt_fixture_build_coverage_no_unresolved_transfer_and_replay",
    "test_dbt_rejects_shared_google_merge_and_preserves_active",
    "test_human_identity_acceptance_does_not_verify_conflicting_category",
)) + (
    "tests/test_area_enrichment.py::test_real_dbt_fixtures_nulls_no_fanout_and_deterministic_rebuild",
    # This guard test stubs execution, but build() still requires installed dbt
    # version metadata for the real publication signature. Keep offline dbt-free.
    "tests/test_area_enrichment.py::test_build_final_human_guard_blocks_fresh_and_reused_activation",
)


_original_connect = socket.socket.connect


def deny_network(*args, **kwargs):
    raise RuntimeError("External network access is disabled for offline tests")


def local_only_connect(sock, address):
    # Permit local event-loop IPC. The curated suites do not launch browsers.
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _original_connect(sock, address)
    return deny_network()


class Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.console.write(text)
        self.log.write(text)
        self.log.flush()
        return len(text)

    def flush(self):
        self.console.flush()
        self.log.flush()

    def __getattr__(self, name):
        return getattr(self.console, name)


class Results:
    def __init__(self, directory, profile, collect_only):
        self.directory, self.profile, self.collect_only = directory, profile, collect_only
        self.counts = Counter()
        self.collected = 0

    def event(self, stage, **details):
        with (self.directory / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(), stage=stage, **details)) + "\n")

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
        # A new test using a known integration fixture must be classified.
        if self.profile == "offline":
            for item in session.items:
                if {"attached_gold", "anchor_dbt_fixture"} & set(item.fixturenames):
                    import pytest
                    raise pytest.UsageError("Unclassified dbt fixture dependency: " + item.nodeid)
        self.event("collected", tests=self.collected)

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.counts["collection_" + report.outcome] += 1
            self.event("collection_" + report.outcome, nodeid=report.nodeid)

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed or report.skipped:
            outcome = "xfail" if hasattr(report, "wasxfail") else report.outcome
            self.counts[outcome] += 1
            self.event("test", nodeid=report.nodeid, phase=report.when, outcome=outcome)

    def pytest_sessionfinish(self, session, exitstatus):
        # Optional-dependency/fixture skips must never turn this profile green.
        if not exitstatus and (self.counts["skipped"] or self.counts["xfail"] or self.counts["collection_skipped"]):
            session.exitstatus = 1


def dependencies(profile):
    required = {p: None for p in ("pytest", "numpy", "pyarrow", "shapely", "duckdb",
                                  "httpx", "beautifulsoup4", "selenium", "playwright", "psutil")}
    if profile == "dbt-integration":
        # Authoritative adapter pins also used by the dbt installation extra.
        for line in (ROOT / "requirements/gold-staging.txt").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                package, expected = line.strip().split("==")
                required[package] = expected
        for project in ("gold_staging", "area_enrichment"):
            for name in ("dbt_project.yml", "profiles.yml"):
                if not (ROOT / "pipelines" / project / name).is_file():
                    raise ValueError("Missing dbt fixture project file: " + project + "/" + name)
    actual = {}
    for package, expected in required.items():
        try:
            actual[package] = version(package)
        except PackageNotFoundError as exc:
            raise ValueError("Missing dependency: " + package + "; install .[test" + (",dbt]" if profile == "dbt-integration" else "]")) from exc
        if expected and actual[package] != expected:
            raise ValueError(f"Wrong {package} version: {actual[package]}; required {expected}")
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("offline", "dbt-integration", "manual-runtime"), default="offline")
    parser.add_argument("--suite", action="append", choices=OFFLINE_SUITES, help="Restrict to a registered suite; repeatable")
    parser.add_argument("--list", action="store_true", help="List explicit selectors without importing pytest or running tests")
    parser.add_argument("--collect-only", action="store_true", help="Validate collection only; summary reports COLLECTED, not PASS")
    parser.add_argument("--results-dir", type=Path, help="Parent for a new unique results directory (default: system temp)")
    args = parser.parse_args(argv)
    selected = args.suite or list(OFFLINE_SUITES)
    selectors = selected if args.profile == "offline" else [p for p in DBT_TESTS if p.split("::")[0] in selected]
    if args.profile == "manual-runtime":
        print("Manual runtime E2E requires compatible pinned releases and a separate scratch runner; see README. Never executed by this runner or CI.")
        return 0 if args.list else 2
    if args.list:
        print(json.dumps(dict(profile=args.profile, selectors=selectors,
                              excluded=list(DBT_TESTS) if args.profile == "offline" else []), indent=2))
        return 0
    if not selectors:
        parser.error("No tests registered for this profile/suite combination")
    os.chdir(ROOT)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    if args.results_dir:
        args.results_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="fnb-" + args.profile + "-", dir=args.results_dir))
    print("Results: " + str(directory), flush=True)
    results = Results(directory, args.profile, args.collect_only)
    summary = dict(profile=args.profile, status="RUNNING", selectors=selectors, collect_only=args.collect_only)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    results.event("started")
    try:
        summary["dependencies"] = dependencies(args.profile)
        for path in selectors:
            if not (ROOT / path.split("::")[0]).is_file():
                raise ValueError("Missing registered suite: " + path)
    except (OSError, ValueError) as exc:
        summary.update(status="BLOCKED", error=str(exc), exit_code=2)
        (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        results.event("blocked", error=str(exc))
        print(str(exc), file=sys.stderr)
        return 2
    socket.create_connection = deny_network
    socket.getaddrinfo = deny_network
    socket.socket.connect = local_only_connect
    socket.socket.connect_ex = deny_network
    socket.socket.sendto = deny_network
    import pytest
    # Older local pytest includes nose compatibility, which mistakes ordinary
    # setup(tmp_path, monkeypatch) helpers for module hooks. These are pytest
    # suites, not nose suites; disabling that plugin preserves their test bodies.
    options = ["-q", "-p", "no:cacheprovider", "-p", "no:nose", "--strict-config", "--strict-markers",
               "--basetemp", str(directory / "fixtures"), "--junitxml", str(directory / "junit.xml")]
    if args.profile == "offline":
        options += ["--deselect=" + node for node in DBT_TESTS]
    if args.collect_only:
        options += ["--collect-only"]
    with (directory / "pytest.log").open("w", encoding="utf-8") as log:
        with redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
            code = int(pytest.main(selectors + options, plugins=[results]))
    summary.update(status=("COLLECTED" if args.collect_only else "PASS") if code == 0 else "FAIL",
                   exit_code=code, collected=results.collected, counts=dict(results.counts))
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    results.event("finished", status=summary["status"], exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
