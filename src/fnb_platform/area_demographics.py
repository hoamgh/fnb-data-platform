"""Compatibility alias for fnb_platform.reference.area_demographics."""
import sys
from .reference import area_demographics as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
