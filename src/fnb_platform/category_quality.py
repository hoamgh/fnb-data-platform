"""Compatibility alias for fnb_platform.integration.category_quality."""
import sys
from .integration import category_quality as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
