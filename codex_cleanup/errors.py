"""Failures are deliberately fail-closed."""


class CleanupError(Exception):
    """No further deletion should be attempted after this error."""


class UnsupportedLayout(CleanupError):
    pass


class StalePlan(CleanupError):
    pass


class UnsafeRuntime(CleanupError):
    pass


class InterruptedRun(CleanupError):
    pass


class ProtocolError(CleanupError):
    pass
