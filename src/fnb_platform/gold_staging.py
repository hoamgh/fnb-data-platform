"""Compatibility alias for fnb_platform.gold.staging."""
import sys
from .gold import staging as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
