"""Tests for :mod:`kolla_otel.extravars`."""

from kolla_otel.config import (
    InstrumentationLanguage,
    OTelConfig,
    ServiceInstrumentationSpec,
)
from kolla_otel.extravars import default_container_name, to_extra_vars


def _spec(name="nova-api", **kw):
    return ServiceInstrumentationSpec(
        name=name, language=InstrumentationLanguage.PYTHON, **kw
    )


# --------------------------------------------------------------------------
# default_container_name
# --------------------------------------------------------------------------
def test_default_container_name_hyphens_to_underscores() -> None:
    """Service names are mapped to kolla's underscored container names."""
    assert default_container_name("nova-api") == "nova_api"
    assert default_container_name("nova-super-conductor") == (
        "nova_super_conductor"
    )


# --------------------------------------------------------------------------
# to_extra_vars
# --------------------------------------------------------------------------
def test_to_extra_vars_maps_config_fields() -> None:
    """Deployment-wide config becomes otel_* variables."""
    config = OTelConfig(
        exporter_endpoint="http://c:4317",
        exporter_protocol="http/protobuf",
        deployment_environment="prod",
        resource_attributes={"team": "core"},
    )
    extra = to_extra_vars(config, [_spec()])
    assert extra["otel_exporter_endpoint"] == "http://c:4317"
    assert extra["otel_exporter_protocol"] == "http/protobuf"
    assert extra["otel_propagators"] == "tracecontext,baggage"
    assert extra["otel_deployment_environment"] == "prod"
    assert extra["otel_resource_attributes_extra"] == {"team": "core"}


def test_to_extra_vars_omits_optional_fields_when_unset() -> None:
    """Deployment environment / extra attributes are omitted when unset."""
    extra = to_extra_vars(
        OTelConfig(exporter_endpoint="http://c:4317"), [_spec()]
    )
    assert "otel_deployment_environment" not in extra
    assert "otel_resource_attributes_extra" not in extra


def test_to_extra_vars_builds_service_entries() -> None:
    """Each enabled spec yields a service entry with a container name."""
    specs = [
        _spec(name="nova-api"),
        _spec(name="keystone", otel_service_name="identity"),
        _spec(name="cinder-api", resource_attributes={"tier": "block"}),
        _spec(name="glance-api", environment={"OTEL_LOG_LEVEL": "debug"}),
    ]
    services = to_extra_vars(
        OTelConfig(exporter_endpoint="http://c:4317"), specs
    )["otel_instrument_services"]

    assert services[0] == {
        "name": "nova-api",
        "container_name": "nova_api",
        "language": "python",
    }
    assert services[1]["otel_service_name"] == "identity"
    assert services[2]["resource_attributes"] == {"tier": "block"}
    assert services[3]["environment"] == {"OTEL_LOG_LEVEL": "debug"}


def test_to_extra_vars_skips_disabled_specs() -> None:
    """Disabled specs are excluded from the service list."""
    specs = [_spec(name="on"), _spec(name="off", enabled=False)]
    services = to_extra_vars(
        OTelConfig(exporter_endpoint="http://c:4317"), specs
    )["otel_instrument_services"]
    assert [s["name"] for s in services] == ["on"]
