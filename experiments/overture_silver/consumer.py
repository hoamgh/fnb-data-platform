"""Historical CLI/import compatibility for fnb_platform.overture.silver."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from fnb_platform.overture import silver as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    # A shared module object preserves exceptions, globals and monkeypatches.
    sys.modules[__name__] = _implementation
