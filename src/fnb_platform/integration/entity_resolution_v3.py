"""Historical import alias; matching_policy owns the sole canonical algorithm."""
import sys
from . import matching_policy as _implementation

sys.modules[__name__] = _implementation
