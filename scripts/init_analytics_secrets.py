"""Generate only missing analytics secrets; existing platform credentials are untouched."""

from pathlib import Path
import secrets

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / ".auth/fnb"
    target.mkdir(parents=True, exist_ok=True)
    for name in [
        "admin_password",
        "analytics_writer_password",
        "analytics_readonly_password",
        "superset_metadata_password",
        "superset_secret_key",
        "superset_admin_password",
    ]:
        path = target / name
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_hex(32))
            path.chmod(0o600)
        except FileExistsError:
            pass
    print("Analytics secrets ready; no credential values printed.")
