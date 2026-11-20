"""Import native Superset assets and configure their read-only connection."""

import json
from pathlib import Path
from zipfile import ZipFile

from superset import db, security_manager
from superset.app import create_app

MARKER = "Managed by fnb analytics v1"
SLUG = "hcmc-fnb-market-overview"


def secret(name):
    return (Path("/run/secrets") / name).read_text().strip()


def chart_ids(layout, node_id):
    """Read the native layout inventory; never generate chart definitions."""
    node = layout[node_id]
    if node["type"] == "CHART":
        return [node["meta"]["chartId"]]
    return [
        value
        for child in node.get("children", [])
        for value in chart_ids(layout, child)
    ]


def configure():
    # ORM imports require Superset's initialized encrypted-field factory.
    from flask import g
    from sqlalchemy.engine import URL
    from superset.commands.dashboard.importers.dispatcher import ImportDashboardsCommand
    from superset.commands.importers.v1.utils import get_contents_from_bundle
    from superset.models.core import Database
    from superset.models.dashboard import Dashboard

    dashboard = db.session.query(Dashboard).filter_by(slug=SLUG).one_or_none()
    if dashboard is not None and MARKER not in (dashboard.description or ""):
        raise ValueError("Existing dashboard is not managed by this project; preserved")
    user = security_manager.find_user(username="portfolio_admin")
    if user is None:
        user = security_manager.add_user(
            username="portfolio_admin",
            first_name="Portfolio",
            last_name="Admin",
            email="portfolio-admin@localhost.invalid",
            role=security_manager.find_role("Admin"),
            password=secret("superset_admin_password"),
        )
    g.user = user
    with ZipFile(Path(__file__).with_name("market_overview.zip")) as bundle:
        contents = get_contents_from_bundle(bundle)
    passwords = {
        name: secret("analytics_readonly_password")
        for name in contents
        if name.startswith("databases/")
    }
    ImportDashboardsCommand(contents, passwords=passwords, overwrite=True).run()

    database = (
        db.session.query(Database)
        .filter_by(database_name="FNB Analytics (read only)")
        .one()
    )
    database.sqlalchemy_uri = URL.create(
        "postgresql+psycopg2",
        username="fnb_analytics_readonly",
        password=secret("analytics_readonly_password"),
        host="postgres",
        database="fnb_analytics",
    ).render_as_string(hide_password=False)
    database.allow_dml = False
    database.expose_in_sqllab = False
    db.session.commit()

    dashboard = db.session.query(Dashboard).filter_by(slug=SLUG).one()
    layout = json.loads(dashboard.position_json)
    tabset = next(
        layout[key]
        for key in layout["GRID_ID"]["children"]
        if layout[key]["type"] == "TABS"
    )
    tabs = [
        {"label": layout[key]["meta"]["text"], "chart_ids": chart_ids(layout, key)}
        for key in tabset["children"]
    ]
    ordered_ids = [value for tab in tabs for value in tab["chart_ids"]]
    charts = {chart.id: chart for chart in dashboard.slices}
    nodes = [
        node
        for node in layout.values()
        if isinstance(node, dict) and node.get("type") == "CHART"
    ]
    if (
        len(ordered_ids) != len(set(ordered_ids))
        or set(ordered_ids) != set(charts)
        or len(nodes) != len(ordered_ids)
        or any(
            node["meta"].get("uuid") != str(charts[node["meta"]["chartId"]].uuid)
            for node in nodes
        )
    ):
        raise ValueError("Imported chart inventory differs from the native dashboard")
    filters = json.loads(dashboard.json_metadata)["native_filter_configuration"]
    report = dict(
        status="CONFIGURED",
        dashboard_id=dashboard.id,
        slug=SLUG,
        charts=[
            dict(id=value, viz_type=charts[value].viz_type) for value in ordered_ids
        ],
        tabs=tabs,
        native_filters=len(filters),
        default_areas=filters[0]
        .get("defaultDataMask", {})
        .get("filterState", {})
        .get("value", []),
        market_metrics=[
            "reference_population",
            "reference_area_km2",
            "observed_stores_per_km2",
            "observed_stores_per_1000_residents",
        ],
        credentials="Secret files only; PostgreSQL serving connection is read-only",
        map_status="AWAITING_BROWSER_VERIFICATION",
    )
    Path("/analytics-out/superset-assets.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            dict(status="CONFIGURED", dashboard_id=dashboard.id, charts=len(charts))
        )
    )


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        configure()
