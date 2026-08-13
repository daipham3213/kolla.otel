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
    Path(__file__).resolve().parents[1]
    / "ansible"
    / "roles"
    / "otel_instrument"
    / "defaults"
    / "main.yml"
)


def _role_defaults() -> dict:
    return yaml.safe_load(_DEFAULTS_YML.read_text(encoding="utf-8"))


def _to_bool(value: object) -> bool:
    """Stub for Ansible's ``bool`` filter (absent from plain Jinja2)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _render_role_services(role_defaults: dict, enables: dict) -> list:
    """Render the role's enable-gated ``otel_instrument_services`` expression.

    Feeds the per-project ``otel_services_*`` lists and the given ``enable_*``
    flags through a native Jinja environment (with a stubbed ``bool`` filter),
    returning the resulting native list — so we can compare it to the Python
    ``default_services()`` the action plugin uses.
    """
    from jinja2.nativetypes import NativeEnvironment

    env = NativeEnvironment()
    env.filters["bool"] = _to_bool
    context = {
        key: value
        for key, value in role_defaults.items()
        if key.startswith("otel_services_")
    }
    context.update(enables)
    return env.from_string(role_defaults["otel_instrument_services"]).render(
        **context
    )


class RoleDefaultsDriftTestCase:
    """Guards keeping the Python default copies in sync with the role."""

    def test_language_defaults_match_role(self) -> None:
        """The Python language table mirrors otel_language_defaults."""
        role = _role_defaults()["otel_language_defaults"]
        assert role == instr.LANGUAGE_DEFAULTS

    def test_service_groups_match_role(self) -> None:
        """Each per-project otel_services_* list mirrors the Python group."""
        role = _role_defaults()
        for project, group in instr.DEFAULT_SERVICE_GROUPS.items():
            assert role[f"otel_services_{project}"] == group["services"]
        # ...and the flattened catalog is exactly DEFAULT_SERVICES.
        flattened = [
            s
            for g in instr.DEFAULT_SERVICE_GROUPS.values()
            for s in g["services"]
        ]
        assert flattened == instr.DEFAULT_SERVICES

    def test_enable_gating_matches_role(self) -> None:
        """The role's otel_instrument_services (gated by enable_* flags)
        renders to the same list the plugin computes via default_services()."""
        role = _role_defaults()
        flags = [
            g["enable_flag"] for g in instr.DEFAULT_SERVICE_GROUPS.values()
        ]
        for enables in (
            dict.fromkeys(flags, True),
            dict.fromkeys(flags, False),
            {**dict.fromkeys(flags, False), "enable_nova": True},
            {**dict.fromkeys(flags, True), "enable_cinder": False},
        ):
            rendered = _render_role_services(role, enables)
            expected = instr.default_services(
                lambda flag, fallback, e=enables: e[flag]
            )
            assert rendered == expected

    def test_eventlet_services_match_role(self) -> None:
        """otel_eventlet_services mirrors DEFAULT_EVENTLET_SERVICES."""
        assert (
            _role_defaults()["otel_eventlet_services"]
            == instr.DEFAULT_EVENTLET_SERVICES
        )

    def test_common_env_map_matches_role(self) -> None:
        """Each COMMON_ENV_MAP entry references the scalar the role uses."""
        role_common = _role_defaults()["otel_common_environment"]
        assert set(role_common) == set(instr.COMMON_ENV_MAP)
        for env_key, scalar in instr.COMMON_ENV_MAP.items():
            # role value is a Jinja ref like "{{ otel_exporter_endpoint }}"
            assert scalar in role_common[env_key]

    def test_scalar_defaults_match_role(self) -> None:
        """SCALAR_DEFAULTS agrees with the role's scalar defaults."""
        role = _role_defaults()
        for var, default in instr.SCALAR_DEFAULTS.items():
            assert str(role[var]) == default

    def test_image_defaults_match_role(self) -> None:
        """Agent image registry/version defaults match the role."""
        role = _role_defaults()
        assert role["otel_image_registry"] == instr.DEFAULT_IMAGE_REGISTRY
        assert str(role["otel_image_version"]) == instr.DEFAULT_IMAGE_VERSION

    def test_local_collector_endpoint_matches_role(self) -> None:
        """The role default resolves this host's api_interface address; when
        no address can be resolved (no facts / interface unset) it falls back
        to the Python constant the action plugin uses."""
        from jinja2 import Environment

        expr = _role_defaults()["otel_local_collector_endpoint"]
        # The role value is now a Jinja expression, not a bare literal.
        assert "{{" in expr
        env = Environment()
        # Stub kolla's put_address_in_context filter (identity for IPv4) so the
        # expression renders without an Ansible templar.
        env.filters["put_address_in_context"] = lambda addr, ctx=None: addr
        rendered = env.from_string(expr).render(ansible_facts={})
        assert rendered == instr.DEFAULT_LOCAL_COLLECTOR_ENDPOINT
        # Default (gRPC) protocol -> gRPC port; the Python fallback agrees.
        assert instr.local_collector_endpoint(None, "grpc") == rendered

    def test_local_collector_endpoint_ports_match_role(self) -> None:
        """The protocol->port choice mirrors the role expression: the HTTP
        receiver port for http/protobuf, else the gRPC port."""
        from jinja2 import Environment

        expr = _role_defaults()["otel_local_collector_endpoint"]
        env = Environment()
        env.filters["put_address_in_context"] = lambda addr, ctx=None: addr
        rendered_http = env.from_string(expr).render(
            ansible_facts={}, otel_exporter_protocol="http/protobuf"
        )
        assert rendered_http == instr.local_collector_endpoint(
            None, "http/protobuf"
        )
        assert rendered_http.endswith(f":{instr.DEFAULT_COLLECTOR_HTTP_PORT}")


class InstrumentationTestCase:
    """The pure overlay-logic functions."""

    def test_agent_image_and_stage_paths(self) -> None:
        assert (
            instr.agent_image(
                "ghcr.io/open-telemetry/opentelemetry-operator/",
                "autoinstrumentation-python",
                "0.50b0",
            )
            == "ghcr.io/open-telemetry/opentelemetry-operator/"
            "autoinstrumentation-python:0.50b0"
        )
        # falsy registry/version fall back to the defaults
        assert instr.agent_image("", "c", "") == (
            f"{instr.DEFAULT_IMAGE_REGISTRY}/c:{instr.DEFAULT_IMAGE_VERSION}"
        )
        assert instr.stage_paths("/etc/kolla/opentelemetry", "python") == (
            "/etc/kolla/opentelemetry/python",
            "/etc/kolla/opentelemetry/.python-image-id",
        )

    def test_deep_merge_is_recursive_and_pure(self) -> None:
        base = {"a": {"x": 1, "y": 2}, "b": 1}
        override = {"a": {"y": 3, "z": 4}, "c": 5}
        merged = instr.deep_merge(base, override)
        assert merged == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}
        assert base == {"a": {"x": 1, "y": 2}, "b": 1}  # unmutated

    def test_python_activation_sets_oslo_service_distro(self) -> None:
        activation = instr.LANGUAGE_DEFAULTS["python"]["activation"]
        assert activation["OTEL_PYTHON_DISTRO"] == "oslo_service"
        assert activation["OTEL_PYTHON_CONFIGURATOR"] == "oslo_service"
        # only Python carries the oslo.service distro
        for lang in ("java", "nodejs", "dotnet"):
            act = instr.LANGUAGE_DEFAULTS[lang]["activation"]
            assert "OTEL_PYTHON_DISTRO" not in act

    def test_default_eventlet_services_classification(self) -> None:
        """Eventlet daemons/servers are listed; uWSGI/mod_wsgi ones are not,
        and every listed name is a real target."""
        eventlet = set(instr.DEFAULT_EVENTLET_SERVICES)
        assert eventlet == {
            "nova-conductor",
            "nova-scheduler",
            "nova-compute",
            "cinder-scheduler",
            "cinder-volume",
            "cinder-backup",
            "neutron-server",
            "glance-api",
            "heat-api",
            "heat-engine",
        }
        catalog = {s["name"] for s in instr.DEFAULT_SERVICES}
        assert eventlet <= catalog  # every eventlet name is a known service
        # the WSGI services are deliberately excluded
        assert eventlet.isdisjoint(
            {"keystone", "nova-api", "cinder-api", "placement-api"}
        )

    def test_default_services_gating(self) -> None:
        """default_services includes only groups whose flag is truthy."""
        assert instr.default_services(lambda flag, default: True) == (
            instr.DEFAULT_SERVICES
        )
        assert instr.default_services(lambda flag, default: False) == []
        # honors the per-group fallback when the resolver defers to it
        only_defaults = instr.default_services(lambda flag, default: default)
        names = {s["name"] for s in only_defaults}
        assert "nova-api" in names  # nova default True
        assert "cinder-api" not in names  # cinder default False
        # gate a single project on
        nova_only = instr.default_services(
            lambda flag, default: flag == "enable_nova"
        )
        assert nova_only == instr.DEFAULT_SERVICE_GROUPS["nova"]["services"]

    def test_eventlet_environment(self) -> None:
        assert instr.eventlet_environment(
            "nova-conductor", instr.DEFAULT_EVENTLET_SERVICES
        ) == {"OTEL_PYTHON_EVENTLET_MONKEY_PATCH": "true"}
        assert (
            instr.eventlet_environment(
                "keystone", instr.DEFAULT_EVENTLET_SERVICES
            )
            == {}
        )
        assert instr.eventlet_environment("nova-conductor", []) == {}
        assert instr.eventlet_environment("nova-conductor", None) == {}

    def test_resolve_language_applies_overrides(self) -> None:
        lang = instr.resolve_language(
            "python", {"python": {"mount_path": "/opt/otel"}}
        )
        assert lang["mount_path"] == "/opt/otel"
        # activation from defaults is preserved by the deep merge
        assert "PYTHONPATH" in lang["activation"]

    def test_resource_attributes_layering_and_string(self) -> None:
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
        assert (
            instr.resource_attributes_string({"b": "2", "a": "1"}) == "a=1,b=2"
        )

    def test_managed_environment_layering_activation_wins(self) -> None:
        env = instr.managed_environment(
            common_env={"OTEL_TRACES_EXPORTER": "otlp"},
            extra_env={"FOO": "bar"},
            service_name="nova-api",
            resource_attrs="service.namespace=openstack",
            service_env={
                "OTEL_TRACES_SAMPLER_ARG": "0.1",
                "PYTHONPATH": "/nope",
            },
            activation={"PYTHONPATH": "/agent"},
        )
        assert env["OTEL_SERVICE_NAME"] == "nova-api"
        assert env["FOO"] == "bar"
        assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.1"
        # activation is applied last and cannot be clobbered by service env
        assert env["PYTHONPATH"] == "/agent"

    def test_apply_agent_mount_replaces_stale_mount(self) -> None:
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

    def test_apply_agent_mount_handles_empty(self) -> None:
        assert instr.apply_agent_mount(None, "/m", "src:/m:ro") == [
            "src:/m:ro"
        ]

    def test_remove_agent_mount_drops_only_the_agent_bind(self) -> None:
        binds = [
            "/etc/kolla/nova:/var/lib/kolla/config_files:ro",
            "/etc/kolla/opentelemetry/python:"
            "/otel-auto-instrumentation-python:ro",
        ]
        assert instr.remove_agent_mount(
            binds, "/otel-auto-instrumentation-python"
        ) == ["/etc/kolla/nova:/var/lib/kolla/config_files:ro"]
        # nothing to drop -> unchanged; None -> empty
        assert instr.remove_agent_mount(binds, "/nope") == binds
        assert instr.remove_agent_mount(None, "/m") == []

    def test_managed_env_keys_mirrors_rollback_possible_keys(self) -> None:
        keys = instr.managed_env_keys(
            ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_TRACES_EXPORTER"],
            ["MY_EXTRA"],
            ["OTEL_TRACES_SAMPLER_ARG"],
            ["PYTHONPATH"],
        )
        assert keys == [
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_TRACES_EXPORTER",
            "MY_EXTRA",
            "OTEL_SERVICE_NAME",
            "OTEL_RESOURCE_ATTRIBUTES",
            "OTEL_TRACES_SAMPLER_ARG",
            "PYTHONPATH",
        ]
        # duplicates across sources are removed, order preserved
        assert instr.managed_env_keys(
            ["OTEL_SERVICE_NAME"], [], [], ["OTEL_SERVICE_NAME"]
        ) == ["OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES"]

    def test_strip_managed_environment_removes_only_named_keys(self) -> None:
        env = {"KOLLA_X": "1", "OTEL_SERVICE_NAME": "s", "PYTHONPATH": "/a"}
        assert instr.strip_managed_environment(
            env, ["OTEL_SERVICE_NAME", "PYTHONPATH"]
        ) == {"KOLLA_X": "1"}
        # base env untouched when nothing matches; None -> {}
        assert instr.strip_managed_environment(env, ["NOPE"]) == env
        assert instr.strip_managed_environment(None, ["x"]) == {}

    def test_agent_bind_and_label(self) -> None:
        assert (
            instr.agent_bind("/etc/kolla/opentelemetry", "python", "/mnt")
            == "/etc/kolla/opentelemetry/python:/mnt:ro"
        )
        assert instr.managed_label_value({"B": "1", "A": "2"}) == "A,B"

    def test_collector_endpoint_port_by_protocol(self) -> None:
        assert instr.collector_endpoint_port("grpc") == 4317
        assert instr.collector_endpoint_port("http/protobuf") == 4318
        # unknown protocols default to the gRPC port
        assert instr.collector_endpoint_port("weird") == 4317
        # explicit ports override the defaults
        assert instr.collector_endpoint_port("http/protobuf", 5317, 5318) == (
            5318
        )

    def test_address_in_url_context_brackets_ipv6(self) -> None:
        assert instr.address_in_url_context("10.0.0.5") == "10.0.0.5"
        assert instr.address_in_url_context("host") == "host"
        assert instr.address_in_url_context("fe80::1") == "[fe80::1]"
        # already bracketed is left untouched
        assert instr.address_in_url_context("[fe80::1]") == "[fe80::1]"

    def test_interface_address_lookup(self) -> None:
        facts = {
            "eth0": {"ipv4": {"address": "10.0.0.5"}},
            "br_ex": {"ipv6": {"address": "fe80::1"}},
        }
        assert instr.interface_address(facts, "eth0") == "10.0.0.5"
        # hyphenated interfaces map to the underscored fact key
        assert instr.interface_address(facts, "br-ex", "ipv6") == "fe80::1"
        # missing interface / family / address -> None
        assert instr.interface_address(facts, "eth1") is None
        assert instr.interface_address(facts, "eth0", "ipv6") is None
        assert instr.interface_address({}, "eth0") is None
        assert instr.interface_address(facts, "") is None

    def test_local_collector_endpoint_builds_url(self) -> None:
        assert instr.local_collector_endpoint("10.0.0.5", "grpc") == (
            "http://10.0.0.5:4317"
        )
        assert (
            instr.local_collector_endpoint("10.0.0.5", "http/protobuf")
            == "http://10.0.0.5:4318"
        )
        # IPv6 address is bracketed
        assert instr.local_collector_endpoint("fe80::1", "grpc") == (
            "http://[fe80::1]:4317"
        )
        # no address -> loopback fallback
        assert instr.local_collector_endpoint(None, "grpc") == (
            instr.DEFAULT_LOCAL_COLLECTOR_ENDPOINT
        )

    def test_find_service_by_container_name_and_fallback(self) -> None:
        services = [
            {
                "name": "nova-api",
                "container_name": "nova_api",
                "language": "py",
            },
            {"name": "keystone", "language": "py"},  # no container_name
        ]
        assert instr.find_service(services, "nova_api")["name"] == "nova-api"
        # falls back to hyphen->underscore of name
        assert instr.find_service(services, "keystone")["name"] == "keystone"
        assert instr.find_service(services, "absent") is None
