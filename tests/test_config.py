"""Tests for :mod:`kolla_otel.config`."""

import pytest

from kolla_otel.config import (
    InstrumentationLanguage,
    OTelConfig,
    ServiceInstrumentationSpec,
    _as_str_mapping,
    load_config,
)
from kolla_otel.exceptions import ConfigurationError


class InstrumentationLanguageTestCase:
    """:class:`InstrumentationLanguage` and ``from_string``."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("python", InstrumentationLanguage.PYTHON),
            ("  JAVA ", InstrumentationLanguage.JAVA),
            ("nodejs", InstrumentationLanguage.NODEJS),
            ("node", InstrumentationLanguage.NODEJS),
            ("js", InstrumentationLanguage.NODEJS),
            ("net", InstrumentationLanguage.DOTNET),
            ("dotnet", InstrumentationLanguage.DOTNET),
        ],
    )
    def test_from_string_accepts_names_and_aliases(
        self, value: str, expected: InstrumentationLanguage
    ) -> None:
        """Canonical names, aliases and surrounding whitespace all resolve."""
        assert InstrumentationLanguage.from_string(value) is expected

    def test_from_string_rejects_unknown(self) -> None:
        """An unknown language raises :class:`ConfigurationError`."""
        with pytest.raises(ConfigurationError):
            InstrumentationLanguage.from_string("cobol")


class OTelConfigTestCase:
    """:class:`OTelConfig` construction and validation."""

    def test_defaults(self) -> None:
        """A minimal config populates sensible defaults."""
        config = OTelConfig(exporter_endpoint="http://c:4317")
        assert config.exporter_protocol == "grpc"
        assert config.propagators == ("tracecontext", "baggage")
        assert config.service_namespace == "openstack"
        assert config.deployment_environment is None

    def test_allows_empty_endpoint_for_local_mode(self) -> None:
        """An empty endpoint is valid — it selects local-collector mode."""
        config = OTelConfig(exporter_endpoint="")
        assert config.exporter_endpoint == ""

    def test_rejects_bad_protocol(self) -> None:
        """Only grpc / http/protobuf are accepted protocols."""
        with pytest.raises(ConfigurationError):
            OTelConfig(
                exporter_endpoint="http://c:4317", exporter_protocol="udp"
            )


class ServiceInstrumentationSpecTestCase:
    """:class:`ServiceInstrumentationSpec` and its ``service_name``."""

    def test_service_name_defaults_to_name(self) -> None:
        """``service_name`` falls back to ``name`` when unset."""
        spec = ServiceInstrumentationSpec(
            name="nova-api", language=InstrumentationLanguage.PYTHON
        )
        assert spec.service_name == "nova-api"

    def test_service_name_uses_override(self) -> None:
        """An explicit ``otel_service_name`` wins over ``name``."""
        spec = ServiceInstrumentationSpec(
            name="keystone",
            language=InstrumentationLanguage.PYTHON,
            otel_service_name="identity",
        )
        assert spec.service_name == "identity"

    def test_rejects_empty_name(self) -> None:
        """A blank service name is invalid."""
        with pytest.raises(ConfigurationError):
            ServiceInstrumentationSpec(
                name="  ", language=InstrumentationLanguage.PYTHON
            )


class AsStrMappingTestCase:
    """The ``_as_str_mapping`` coercion helper."""

    def test_coerces_values(self) -> None:
        """Keys and values are stringified."""
        assert _as_str_mapping({"a": 1, 2: "b"}, "ctx") == {
            "a": "1",
            "2": "b",
        }

    def test_rejects_non_mapping(self) -> None:
        """A non-mapping input raises with the supplied context."""
        with pytest.raises(ConfigurationError):
            _as_str_mapping(["not", "a", "map"], "ctx")


@pytest.fixture
def valid_document() -> dict:
    """Return a minimal valid configuration document."""
    return {
        "otel": {
            "exporter_endpoint": "http://collector:4317",
            "deployment_environment": "production",
            "propagators": ["tracecontext"],
            "resource_attributes": {"service.version": 2},
        },
        "services": [
            {"name": "nova-api", "language": "python"},
            {"name": "cinder", "language": "python", "enabled": False},
        ],
    }


class LoadConfigTestCase:
    """The ``load_config`` document validator."""

    def test_happy_path(self, valid_document: dict) -> None:
        """A valid document produces a config and specs."""
        config, specs = load_config(valid_document)
        assert config.exporter_endpoint == "http://collector:4317"
        assert config.propagators == ("tracecontext",)
        assert config.resource_attributes == {"service.version": "2"}
        assert [s.name for s in specs] == ["nova-api", "cinder"]
        assert specs[1].enabled is False

    def test_parses_per_service_environment(self) -> None:
        """A service's `environment` map is parsed and stringified."""
        _, specs = load_config(
            {
                "otel": {"exporter_endpoint": "http://c:4317"},
                "services": [
                    {
                        "name": "nova-api",
                        "language": "python",
                        "environment": {"OTEL_TRACES_SAMPLER_ARG": 0.1},
                    }
                ],
            }
        )
        assert specs[0].environment == {"OTEL_TRACES_SAMPLER_ARG": "0.1"}

    def test_rejects_non_mapping_root(self) -> None:
        """A non-mapping document is invalid."""
        with pytest.raises(ConfigurationError):
            load_config(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_requires_otel_section(self) -> None:
        """The 'otel' section is mandatory."""
        with pytest.raises(ConfigurationError):
            load_config({"services": [{"name": "x", "language": "python"}]})

    def test_allows_missing_endpoint_for_local_mode(self) -> None:
        """A missing endpoint is allowed and yields local-collector mode."""
        config, _specs = load_config(
            {"otel": {}, "services": [{"name": "x", "language": "python"}]}
        )
        assert config.exporter_endpoint == ""

    def test_rejects_non_string_endpoint(self) -> None:
        """A non-string endpoint is still rejected."""
        with pytest.raises(ConfigurationError):
            load_config(
                {
                    "otel": {"exporter_endpoint": 4317},
                    "services": [{"name": "x", "language": "python"}],
                }
            )

    def test_rejects_unknown_otel_key(self) -> None:
        """Typos in the otel section fail fast."""
        with pytest.raises(ConfigurationError):
            load_config(
                {
                    "otel": {"exporter_endpoint": "e", "sampler_rate": "1"},
                    "services": [{"name": "x", "language": "python"}],
                }
            )

    def test_rejects_non_list_propagators(self) -> None:
        """Propagators must be a list, not a scalar."""
        with pytest.raises(ConfigurationError):
            load_config(
                {
                    "otel": {
                        "exporter_endpoint": "e",
                        "propagators": "tracecontext",
                    },
                    "services": [{"name": "x", "language": "python"}],
                }
            )

    def test_requires_non_empty_services(self) -> None:
        """At least one service must be listed."""
        with pytest.raises(ConfigurationError):
            load_config({"otel": {"exporter_endpoint": "e"}, "services": []})

    @pytest.mark.parametrize(
        "service",
        [
            "not-a-mapping",
            {"language": "python"},  # missing name
            {"name": "x"},  # missing language
            {"name": "x", "language": "python", "otel_service_name": 5},
        ],
    )
    def test_validates_each_service(self, service) -> None:
        """Malformed service entries are rejected."""
        with pytest.raises(ConfigurationError):
            load_config(
                {"otel": {"exporter_endpoint": "e"}, "services": [service]}
            )
