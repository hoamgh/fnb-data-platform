"""Historical CLI/import compatibility for fnb_platform.overture.eligibility."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
# Preserve the root/src bootstrap previously provided by Eligibility + Silver.
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from fnb_platform.overture import eligibility as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    # Keep private APIs and monkeypatch globals on one canonical module object.
    sys.modules[__name__] = _implementation
