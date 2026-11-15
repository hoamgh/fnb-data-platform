"""Historical CLI/import compatibility for fnb_platform.overture.comparison_run."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from fnb_platform.overture import comparison_run as _implementation

if __name__ == "__main__":
    _implementation.cli()
else:
    # Preserve one module object, including private APIs and monkeypatch targets.
    sys.modules[__name__] = _implementation
