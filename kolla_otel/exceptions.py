"""Custom exception hierarchy for :mod:`kolla_otel`.

All errors raised by this package derive from :class:`KollaOtelError`, so
callers can catch every domain-specific failure with a single ``except``
clause while still being able to discriminate between concrete failure
modes when needed.
"""

__all__ = [
    "KollaOtelError",
    "ConfigurationError",
]


class KollaOtelError(Exception):
    """Base class for every error raised by :mod:`kolla_otel`."""


class ConfigurationError(KollaOtelError):
    """Raised when user-supplied configuration is missing or malformed."""
