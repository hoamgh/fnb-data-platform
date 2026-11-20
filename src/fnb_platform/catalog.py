"""Compatibility entrypoint; canonical implementation is fnb_platform.overture.catalog."""
import sys
from .overture import catalog as _implementation

sys.modules[__name__] = _implementation
