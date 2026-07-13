"""Tests for :mod:`kolla_otel.instrumentation`.

Covers the pure overlay logic and, crucially, a drift guard that asserts the
Python copies of the role defaults (:data:`LANGUAGE_DEFAULTS`,
:data:`COMMON_ENV_MAP`, :data:`SCALAR_DEFAULTS`, :data:`DEFAULT_SERVICES`) stay
identical to the ``otel_instrument`` role's ``defaults/main.yml`` — the two are
kept apart only so the action plugin can run when the role's defaults are not
loaded (a plain ``deploy``), and must never diverge.
"""

from pathlib import Path

import yaml

from kolla_otel import instrumentation as instr

_DEFAULTS_YML = (
    Path(__file__).resolve().parents[2]
    / "ansible"
    / "roles"
    / "otel_instrument"
    / "defaults"
    / "main.yml"
)


def _role_defaults() -> dict:
    return yaml.safe_load(_DEFAULTS_YML.read_text(encoding="utf-8"))


# --- drift guards -----------------------------------------------------------


def test_language_defaults_match_role() -> None:
    """The Python language table mirrors otel_language_defaults exactly."""
    role = _role_defaults()["otel_language_defaults"]
    assert role == instr.LANGUAGE_DEFAULTS


def test_default_services_match_role() -> None:
    """The Python default service list mirrors otel_instrument_services."""
    role = _role_defaults()["otel_instrument_services"]
    assert role == instr.DEFAULT_SERVICES


def test_common_env_map_matches_role() -> None:
    """Each COMMON_ENV_MAP entry references the same scalar the role uses."""
    role_common = _role_defaults()["otel_common_environment"]
    assert set(role_common) == set(instr.COMMON_ENV_MAP)
    for env_key, scalar in instr.COMMON_ENV_MAP.items():
        # role value is a Jinja ref like "{{ otel_exporter_endpoint }}"
        assert scalar in role_common[env_key]


def test_scalar_defaults_match_role() -> None:
    """SCALAR_DEFAULTS agrees with the role's scalar defaults."""
    role = _role_defaults()
    for var, default in instr.SCALAR_DEFAULTS.items():
        assert str(role[var]) == default


# --- overlay logic ----------------------------------------------------------


def test_deep_merge_is_recursive_and_pure() -> None:
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    override = {"a": {"y": 3, "z": 4}, "c": 5}
    merged = instr.deep_merge(base, override)
    assert merged == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}
    assert base == {"a": {"x": 1, "y": 2}, "b": 1}  # unmutated


def test_resolve_language_applies_overrides() -> None:
    lang = instr.resolve_language(
        "python", {"python": {"mount_path": "/opt/otel"}}
    )
    assert lang["mount_path"] == "/opt/otel"
    # activation from defaults is preserved by the deep merge
    assert "PYTHONPATH" in lang["activation"]


def test_resource_attributes_layering_and_string() -> None:
    attrs = instr.resource_attributes(
        "openstack", "prod", {"team": "core"}, {"service.tier": "identity"}
    )
    assert attrs == {
        "service.namespace": "openstack",
        "deployment.environment": "prod",
        "team": "core",
        "service.tier": "identity",
    }
    # deployment.environment omitted when empty
    assert "deployment.environment" not in instr.resource_attributes(
        "openstack", "", {}, {}
    )
    # rendered sorted by key
    assert instr.resource_attributes_string({"b": "2", "a": "1"}) == "a=1,b=2"


def test_managed_environment_layering_activation_wins() -> None:
    env = instr.managed_environment(
        common_env={"OTEL_TRACES_EXPORTER": "otlp"},
        extra_env={"FOO": "bar"},
        service_name="nova-api",
        resource_attrs="service.namespace=openstack",
        service_env={"OTEL_TRACES_SAMPLER_ARG": "0.1", "PYTHONPATH": "/nope"},
        activation={"PYTHONPATH": "/agent"},
    )
    assert env["OTEL_SERVICE_NAME"] == "nova-api"
    assert env["FOO"] == "bar"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.1"
    # activation is applied last and cannot be clobbered by service env
    assert env["PYTHONPATH"] == "/agent"


def test_apply_agent_mount_replaces_stale_mount() -> None:
    binds = [
        "/etc/kolla/nova:/var/lib/kolla/config_files:ro",
        "old-volume:/otel-auto-instrumentation-python:ro",
    ]
    mount = "/otel-auto-instrumentation-python"
    bind = "/etc/kolla/opentelemetry/python:" + mount + ":ro"
    result = instr.apply_agent_mount(binds, mount, bind)
    assert result == [
        "/etc/kolla/nova:/var/lib/kolla/config_files:ro",
        bind,
    ]


def test_apply_agent_mount_handles_empty() -> None:
    assert instr.apply_agent_mount(None, "/m", "src:/m:ro") == ["src:/m:ro"]


def test_agent_bind_and_label() -> None:
    assert (
        instr.agent_bind("/etc/kolla/opentelemetry", "python", "/mnt")
        == "/etc/kolla/opentelemetry/python:/mnt:ro"
    )
    assert instr.managed_label_value({"B": "1", "A": "2"}) == "A,B"


def test_find_service_by_container_name_and_fallback() -> None:
    services = [
        {"name": "nova-api", "container_name": "nova_api", "language": "py"},
        {"name": "keystone", "language": "py"},  # no explicit container_name
    ]
    assert instr.find_service(services, "nova_api")["name"] == "nova-api"
    # falls back to hyphen->underscore of name
    assert instr.find_service(services, "keystone")["name"] == "keystone"
    assert instr.find_service(services, "absent") is None
