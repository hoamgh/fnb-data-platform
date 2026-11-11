"""Legacy model identity and pickle path; field/method definitions live in fnb_platform."""
from dataclasses import dataclass

from fnb_platform.models import CanonicalPlace as _CanonicalPlace


@dataclass(frozen=True)
class CanonicalPlace(_CanonicalPlace):
    """Keep the historical class name and class-specific dataclass equality."""
