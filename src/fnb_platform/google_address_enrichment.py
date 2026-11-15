"""Compatibility alias for fnb_platform.google.address_enrichment."""
import sys
from .google import address_enrichment as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
