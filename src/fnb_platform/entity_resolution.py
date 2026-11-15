"""Compatibility alias for fnb_platform.integration.entity_resolution."""
import sys
from .integration import entity_resolution as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
