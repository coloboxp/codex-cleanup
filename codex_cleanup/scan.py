"""The public, read-only planning entry point."""

from .models import Settings
from .policy import build_plan
from .storage import snapshot


def scan(settings=None):
    settings = settings or Settings.default()
    return build_plan(settings, snapshot(settings))
