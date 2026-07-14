"""Tests for :mod:`kolla_otel.exceptions`."""

import pytest

from kolla_otel.exceptions import ConfigurationError, KollaOtelError


class ExceptionHierarchyTestCase:
    """The package's exception hierarchy."""

    def test_base_is_an_exception(self) -> None:
        """The base error derives from the built-in ``Exception``."""
        assert issubclass(KollaOtelError, Exception)

    def test_configuration_error_derives_from_base(self) -> None:
        """``ConfigurationError`` is catchable as :class:`KollaOtelError`."""
        assert issubclass(ConfigurationError, KollaOtelError)
        with pytest.raises(KollaOtelError):
            raise ConfigurationError("boom")
