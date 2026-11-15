"""Compatibility alias for fnb_platform.gold.area_enrichment."""
import sys
from .gold import area_enrichment as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
