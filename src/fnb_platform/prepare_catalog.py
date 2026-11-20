"""Compatibility entrypoint; canonical implementation is fnb_platform.overture.acquisition."""
import sys
from .overture import acquisition as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
