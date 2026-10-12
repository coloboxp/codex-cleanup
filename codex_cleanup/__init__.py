"""Alpha, dependency-free Codex archive cleanup."""

from .artifacts import load_manifest, write_manifest
from .errors import CleanupError
from .execution import apply, verify
from .models import VERSION, CleanupPlan, Settings
from .runtime import doctor
from .scan import scan

__version__ = VERSION
__all__ = [
    "CleanupError",
    "CleanupPlan",
    "Settings",
    "apply",
    "doctor",
    "load_manifest",
    "scan",
    "verify",
    "write_manifest",
]
