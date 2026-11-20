"""Compatibility alias for fnb_platform.integration.entity_resolution_v2."""
import sys
from .integration import entity_resolution_v2 as _implementation

sys.modules[__name__] = _implementation
