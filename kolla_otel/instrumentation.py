"""Pure functions that compute the OpenTelemetry instrumentation overlay.

This is the single Python source of truth for *what* instrumenting a
container means — the ``OTEL_*`` environment, the agent bind-mount and the
managed-env label. It is consumed by the ``kolla_container`` action plugin
(``ansible/action_plugins/kolla_container.py``), which re-applies the overlay
whenever kolla itself (re)creates a container during ``deploy`` /
``reconfigure`` — so instrumentation survives operations that recreate
services from kolla's own definitions, without a wrapper command.

The logic here mirrors the ``otel_instrument`` role's ``inject.yml`` exactly.
The role remains the source of truth for the *defaults* (in
``defaults/main.yml``); :data:`LANGUAGE_DEFAULTS` and :data:`COMMON_ENV_MAP`
duplicate a handful of those defaults in Python so the plugin can work during a
plain ``deploy`` (when the role's ``defaults/`` are not loaded). A test
(``test_instrumentation.py``) parses ``defaults/main.yml`` and asserts the two
copies stay in sync, so they cannot silently drift.

The functions are intentionally dependency-free (standard library only) so the
domain logic can be unit-tested without Ansible.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "AUGMENT_ACTIONS",
    "LANGUAGE_DEFAULTS",
    "COMMON_ENV_MAP",
    "SCALAR_DEFAULTS",
    "DEFAULT_SERVICE_GROUPS",
    "DEFAULT_SERVICES",
    "DEFAULT_EVENTLET_SERVICES",
    "EVENTLET_ENV_KEY",
    "default_services",
    "eventlet_environment",
    "DEFAULT_HOST_LIB_PATH",
    "DEFAULT_MANAGED_ENV_LABEL",
    "DEFAULT_IMAGE_REGISTRY",
    "DEFAULT_IMAGE_VERSION",
    "DEFAULT_COLLECTOR_GRPC_PORT",
    "DEFAULT_COLLECTOR_HTTP_PORT",
    "DEFAULT_LOCAL_COLLECTOR_ENDPOINT",
    "deep_merge",
    "resolve_language",
    "resource_attributes",
    "resource_attributes_string",
    "managed_environment",
    "agent_image",
    "stage_paths",
    "agent_bind",
    "apply_agent_mount",
    "remove_agent_mount",
    "managed_env_keys",
    "strip_managed_environment",
    "managed_label_value",
    "find_service",
    "collector_endpoint_port",
    "address_in_url_context",
    "interface_address",
    "local_collector_endpoint",
]

#: ``kolla_container`` actions whose desired spec we augment. Every other
#: action (stop/remove/facts/…) is passed through untouched.
#:
#: ``compare_container`` is included on purpose: kolla decides whether to
#: (re)create a container by comparing its own desired spec against the running
#: one and only then notifying its restart handler. Augmenting the comparison
#: with the OTEL env/mount/label makes kolla notice the missing instrumentation
#: and fire that handler — whose ``recreate_or_restart_container`` we also
#: augment. Once instrumented the comparison matches again, so no needless
#: recreate happens on subsequent runs.
AUGMENT_ACTIONS = frozenset(
    {
        "compare_container",
        "start_container",
        "recreate_or_restart_container",
    }
)

DEFAULT_HOST_LIB_PATH = "/etc/kolla/opentelemetry"
DEFAULT_MANAGED_ENV_LABEL = "kolla_otel.managed_env"

#: Agent image source. Mirrors otel_image_registry / otel_image_version in the
#: role defaults (kept in sync by test_instrumentation.py).
DEFAULT_IMAGE_REGISTRY = "ghcr.io/open-telemetry/opentelemetry-operator"
DEFAULT_IMAGE_VERSION = "latest"

#: Local collector listener ports. Mirror otel_collector_grpc_port /
#: otel_collector_http_port (the otel_collector role defaults, also the
#: inline fallbacks in otel_local_collector_endpoint). The port the
#: instrumented services export to is chosen from these by protocol.
DEFAULT_COLLECTOR_GRPC_PORT = 4317
DEFAULT_COLLECTOR_HTTP_PORT = 4318

#: Fallback endpoint for the per-host local collector (deployed by the
#: otel_collector role, reachable because kolla uses host networking) when no
#: external collector is configured and this host's api_interface address
#: cannot be resolved. Mirrors what the role default
#: (otel_local_collector_endpoint) renders to under the default gRPC protocol
#: when no interface address is available (kept in sync by
#: test_instrumentation.py). The action plugin normally resolves the routable
#: api_interface address (gathering network facts on demand, like the role) and
#: the protocol-appropriate port via :func:`local_collector_endpoint`; this
#: literal is only the last-resort fallback.
DEFAULT_LOCAL_COLLECTOR_ENDPOINT = (
    f"http://127.0.0.1:{DEFAULT_COLLECTOR_GRPC_PORT}"
)

#: Per-language agent definition. Mirrors ``otel_language_defaults`` in the
#: role's ``defaults/main.yml`` (kept in sync by test_instrumentation.py).
LANGUAGE_DEFAULTS: dict[str, dict[str, Any]] = {
    "python": {
        "image_component": "autoinstrumentation-python",
        "source_path": "/autoinstrumentation",
        "mount_path": "/otel-auto-instrumentation-python",
        "activation": {
            "PYTHONPATH": (
                "/otel-auto-instrumentation-python/opentelemetry/"
                "instrumentation/auto_instrumentation:"
                "/otel-auto-instrumentation-python"
            ),
            # OpenStack services build on oslo.service; its OpenTelemetry
            # distro/configurator wires the SDK up the way OpenStack expects
            # (config via oslo.config, correct service naming, …). Applied as
            # activation so it is always set for the Python agent.
            "OTEL_PYTHON_DISTRO": "oslo_service",
            "OTEL_PYTHON_CONFIGURATOR": "oslo_service",
        },
    },
    "java": {
        "image_component": "autoinstrumentation-java",
        "source_path": "/javaagent.jar",
        "mount_path": "/otel-auto-instrumentation-java",
        "activation": {
            "JAVA_TOOL_OPTIONS": (
                "-javaagent:/otel-auto-instrumentation-java/javaagent.jar"
            ),
        },
    },
    "nodejs": {
        "image_component": "autoinstrumentation-nodejs",
        "source_path": "/autoinstrumentation",
        "mount_path": "/otel-auto-instrumentation-nodejs",
        "activation": {
            "NODE_OPTIONS": (
                "--require /otel-auto-instrumentation-nodejs/"
                "autoinstrumentation.js"
            ),
        },
    },
    "dotnet": {
        "image_component": "autoinstrumentation-dotnet",
        "source_path": "/autoinstrumentation",
        "mount_path": "/otel-auto-instrumentation-dotnet",
        "activation": {
            "CORECLR_ENABLE_PROFILING": "1",
            "CORECLR_PROFILER": "{918728DD-259F-4A6A-AC2B-B85E1B658318}",
            "CORECLR_PROFILER_PATH": (
                "/otel-auto-instrumentation-dotnet/linux-x64/"
                "OpenTelemetry.AutoInstrumentation.Native.so"
            ),
            "DOTNET_STARTUP_HOOKS": (
                "/otel-auto-instrumentation-dotnet/net/"
                "OpenTelemetry.AutoInstrumentation.StartupHook.dll"
            ),
            "DOTNET_ADDITIONAL_DEPS": (
                "/otel-auto-instrumentation-dotnet/AdditionalDeps"
            ),
            "DOTNET_SHARED_STORE": ("/otel-auto-instrumentation-dotnet/store"),
            "OTEL_DOTNET_AUTO_HOME": "/otel-auto-instrumentation-dotnet",
        },
    },
}

#: Maps each shared ``OTEL_*`` export variable to the scalar ``otel_*`` var it
#: derives from. Mirrors ``otel_common_environment`` in the role defaults
#: (kept in sync by test_instrumentation.py).
COMMON_ENV_MAP: dict[str, str] = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "otel_exporter_endpoint",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "otel_exporter_protocol",
    "OTEL_TRACES_EXPORTER": "otel_traces_exporter",
    "OTEL_METRICS_EXPORTER": "otel_metrics_exporter",
    "OTEL_LOGS_EXPORTER": "otel_logs_exporter",
    "OTEL_TRACES_SAMPLER": "otel_traces_sampler",
    "OTEL_TRACES_SAMPLER_ARG": "otel_traces_sampler_arg",
    "OTEL_PROPAGATORS": "otel_propagators",
}

#: Scalar ``otel_*`` vars and their role defaults. Used by the action plugin
#: to reconstruct the common OTEL_* environment during a plain ``deploy`` when
#: values are absent from globals.yml (sync-checked by test_instrumentation).
SCALAR_DEFAULTS: dict[str, str] = {
    "otel_exporter_endpoint": "",
    "otel_exporter_protocol": "grpc",
    "otel_traces_exporter": "otlp",
    "otel_metrics_exporter": "otlp",
    "otel_logs_exporter": "otlp",
    "otel_traces_sampler": "parentbased_traceidratio",
    "otel_traces_sampler_arg": "1.0",
    "otel_propagators": "tracecontext,baggage",
    "otel_service_namespace": "openstack",
    "otel_deployment_environment": "",
}


#: Default target services grouped by the OpenStack project (and the kolla
#: ``enable_<project>`` flag that gates it), so the effective target list
#: tracks what kolla actually deployed — enabling a project instruments its
#: services, disabling it drops them, like ``enable_octavia`` deploys octavia.
#: ``enable_default`` is the fallback used only when the flag is absent from
#: the run's variables (in a real kolla run every ``enable_*`` is defined).
#: Mirrors
#: the per-project ``otel_services_*`` vars in the role defaults (kept in sync
#: by test_instrumentation.py).
DEFAULT_SERVICE_GROUPS: dict[str, dict[str, Any]] = {
    "keystone": {
        "enable_flag": "enable_keystone",
        "enable_default": True,
        "services": [
            {
                "name": "keystone",
                "container_name": "keystone",
                "language": "python",
            },
        ],
    },
    "nova": {
        "enable_flag": "enable_nova",
        "enable_default": True,
        "services": [
            {
                "name": "nova-api",
                "container_name": "nova_api",
                "language": "python",
            },
            {
                "name": "nova-conductor",
                "container_name": "nova_conductor",
                "language": "python",
            },
            {
                "name": "nova-scheduler",
                "container_name": "nova_scheduler",
                "language": "python",
            },
            {
                "name": "nova-compute",
                "container_name": "nova_compute",
                "language": "python",
            },
        ],
    },
    "cinder": {
        "enable_flag": "enable_cinder",
        "enable_default": False,
        "services": [
            {
                "name": "cinder-api",
                "container_name": "cinder_api",
                "language": "python",
            },
            {
                "name": "cinder-scheduler",
                "container_name": "cinder_scheduler",
                "language": "python",
            },
            {
                "name": "cinder-volume",
                "container_name": "cinder_volume",
                "language": "python",
            },
            {
                "name": "cinder-backup",
                "container_name": "cinder_backup",
                "language": "python",
            },
        ],
    },
    "glance": {
        "enable_flag": "enable_glance",
        "enable_default": True,
        "services": [
            {
                "name": "glance-api",
                "container_name": "glance_api",
                "language": "python",
            },
        ],
    },
    "neutron": {
        "enable_flag": "enable_neutron",
        "enable_default": True,
        "services": [
            {
                "name": "neutron-server",
                "container_name": "neutron_server",
                "language": "python",
            },
        ],
    },
    "placement": {
        "enable_flag": "enable_placement",
        "enable_default": True,
        "services": [
            {
                "name": "placement-api",
                "container_name": "placement_api",
                "language": "python",
            },
        ],
    },
    "heat": {
        "enable_flag": "enable_heat",
        "enable_default": True,
        "services": [
            {
                "name": "heat-api",
                "container_name": "heat_api",
                "language": "python",
            },
            {
                "name": "heat-engine",
                "container_name": "heat_engine",
                "language": "python",
            },
        ],
    },
}

#: The full, ungated catalog (every group flattened), handy for reference and
#: tests. The plugin uses :func:`default_services` to gate this by the flags.
DEFAULT_SERVICES: list[dict[str, Any]] = [
    dict(service)
    for group in DEFAULT_SERVICE_GROUPS.values()
    for service in group["services"]
]

#: Service names (the hyphenated ``name``) whose Python process runs under
#: eventlet and therefore needs the agent to monkey-patch before instrumenting.
#: The RPC/worker daemons and the eventlet-based API servers; NOT the
#: uWSGI/mod_wsgi services (keystone, nova-api, cinder-api, placement-api),
#: where monkey-patching would be wrong. Mirrors ``otel_eventlet_services`` in
#: the role defaults (kept in sync by test_instrumentation.py).
#: The env var that tells the Python agent to eventlet-monkey-patch first.
EVENTLET_ENV_KEY = "OTEL_PYTHON_EVENTLET_MONKEY_PATCH"

DEFAULT_EVENTLET_SERVICES: list[str] = [
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
]


def deep_merge(
    base: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` (Ansible combine-style).

    Nested mappings are merged; every other value in ``override`` replaces the
    one in ``base``. Neither input is mutated.
    """
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def resolve_language(
    language: str, overrides: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return the effective language definition, applying user overrides.

    :param language: Language key (``python``, ``java``, …).
    :param overrides: The role's ``otel_languages`` mapping, deep-merged onto
        :data:`LANGUAGE_DEFAULTS` — mirrors the role's own deep merge.
    :raises KeyError: If ``language`` is not a known language.
    """
    base = LANGUAGE_DEFAULTS[language]
    override = (overrides or {}).get(language, {}) if overrides else {}
    return deep_merge(base, override or {})


def resource_attributes(
    namespace: str,
    deployment_environment: str,
    extra: Mapping[str, str],
    service_attributes: Mapping[str, str],
) -> dict[str, str]:
    """Build the merged resource-attribute map for one service.

    Layered lowest → highest: ``service.namespace``, an optional
    ``deployment.environment``, deployment-wide extras, then the service's own
    attributes — matching ``inject.yml``.
    """
    attrs: dict[str, str] = {"service.namespace": namespace}
    if deployment_environment:
        attrs["deployment.environment"] = deployment_environment
    attrs.update(extra or {})
    attrs.update(service_attributes or {})
    return attrs


def resource_attributes_string(attrs: Mapping[str, str]) -> str:
    """Render resource attributes as a sorted ``key=value,...`` string."""
    return ",".join(f"{key}={attrs[key]}" for key in sorted(attrs))


def managed_environment(
    common_env: Mapping[str, str],
    extra_env: Mapping[str, str],
    service_name: str,
    resource_attrs: str,
    service_env: Mapping[str, str],
    activation: Mapping[str, str],
) -> dict[str, str]:
    """Compute the full set of env this role manages for one service.

    Layered lowest → highest exactly like ``inject.yml``: common ``OTEL_*``
    export vars, deployment-wide extra env, the service identity, the service's
    own extra env, and the language activation last (so the agent always
    loads and activation can never be clobbered).
    """
    env: dict[str, str] = dict(common_env)
    env.update(extra_env or {})
    env["OTEL_SERVICE_NAME"] = service_name
    env["OTEL_RESOURCE_ATTRIBUTES"] = resource_attrs
    env.update(service_env or {})
    env.update(activation or {})
    return env


def agent_image(registry: str, component: str, version: str) -> str:
    """Return the fully-qualified auto-instrumentation image reference.

    Mirrors ``stage.yml``: ``<registry>/<image_component>:<version>`` with any
    trailing slash on the registry stripped.
    """
    base = (registry or DEFAULT_IMAGE_REGISTRY).rstrip("/")
    return f"{base}/{component}:{version or DEFAULT_IMAGE_VERSION}"


def stage_paths(host_lib_path: str, language: str) -> tuple[str, str]:
    """Return ``(stage_dir, marker_path)`` for a language on the host.

    The agent artifacts live under ``<host_lib_path>/<language>`` and the
    staged image id is recorded in ``<host_lib_path>/.<language>-image-id`` —
    matching ``stage.yml`` so the role and the action plugin stage identically.
    """
    base = host_lib_path or DEFAULT_HOST_LIB_PATH
    return f"{base}/{language}", f"{base}/.{language}-image-id"


def agent_bind(host_lib_path: str, language: str, mount_path: str) -> str:
    """Return the read-only bind string for a language's staged agent."""
    return f"{host_lib_path}/{language}:{mount_path}:ro"


def remove_agent_mount(
    binds: Sequence[str] | None, mount_path: str
) -> list[str]:
    """Return ``binds`` with any bind whose destination is ``mount_path`` gone.

    Drops any existing bind (named volume or host path) targeting
    ``mount_path`` — the agent mount — mirroring the reject filter in
    ``inject.yml`` / ``rollback.yml``. Used both to replace a stale mount
    before adding ours and to strip it entirely on de-instrumentation.
    """
    pattern = re.compile(r"^[^:]+:" + re.escape(mount_path) + r"(:.*)?$")
    return [b for b in (binds or []) if not pattern.match(b)]


def apply_agent_mount(
    binds: Sequence[str] | None, mount_path: str, bind: str
) -> list[str]:
    """Return ``binds`` with any existing mount at ``mount_path`` replaced.

    Drops any existing bind whose destination is ``mount_path`` (a mount left
    by an earlier run, named volume or host path) before appending ``bind``,
    so a recreate never hits "Duplicate mount point" — mirrors ``inject.yml``.
    """
    kept = remove_agent_mount(binds, mount_path)
    kept.append(bind)
    return kept


def managed_env_keys(
    common_env_keys: Iterable[str],
    extra_env_keys: Iterable[str],
    service_env_keys: Iterable[str],
    activation_keys: Iterable[str],
) -> list[str]:
    """Return every env var name this project could manage for a service.

    Computed from names alone (no exporter endpoint needed), mirroring
    ``rollback.yml``'s ``otel_possible_keys``: the shared ``OTEL_*`` export
    vars, deployment-wide extra env, the service identity vars
    (``OTEL_SERVICE_NAME`` / ``OTEL_RESOURCE_ATTRIBUTES``), the service's own
    extra env and the language activation env. Used to strip instrumentation
    without consulting the running container's recorded label. Order is
    preserved and duplicates removed.
    """
    ordered = [
        *common_env_keys,
        *extra_env_keys,
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
        *service_env_keys,
        *activation_keys,
    ]
    seen: set[str] = set()
    result: list[str] = []
    for key in ordered:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def strip_managed_environment(
    environment: Mapping[str, str], keys: Iterable[str]
) -> dict[str, str]:
    """Return ``environment`` with every name in ``keys`` removed.

    Leaves the base-image and kolla env untouched — mirrors the
    ``rejectattr('key', 'in', ...)`` in ``rollback.yml``.
    """
    remove = set(keys)
    return {k: v for k, v in (environment or {}).items() if k not in remove}


def managed_label_value(managed_env: Mapping[str, str]) -> str:
    """Return the sorted, comma-joined managed-env key list for the label."""
    return ",".join(sorted(managed_env.keys()))


def find_service(
    services: Sequence[Mapping[str, Any]], container_name: str
) -> dict[str, Any] | None:
    """Return the service spec whose ``container_name`` matches, or ``None``.

    Entries without an explicit ``container_name`` fall back to the kolla
    convention of the hyphenated ``name`` with underscores.
    """
    for service in services or []:
        name = service.get("name", "")
        candidate = service.get("container_name") or name.replace("-", "_")
        if candidate == container_name:
            return dict(service)
    return None


def default_services(
    is_enabled: Callable[[str, bool], bool],
) -> list[dict[str, Any]]:
    """Return the enable-gated default target list.

    Includes a project group's services only when its ``enable_<project>`` flag
    is truthy, so the list tracks what kolla deployed. ``is_enabled`` is called
    as ``is_enabled(flag_name, fallback)`` and should resolve the flag from the
    run's variables, using ``fallback`` when it is absent. Mirrors the
    ``otel_instrument_services`` composition in the role defaults.
    """
    result: list[dict[str, Any]] = []
    for group in DEFAULT_SERVICE_GROUPS.values():
        if is_enabled(group["enable_flag"], group["enable_default"]):
            result.extend(dict(service) for service in group["services"])
    return result


def eventlet_environment(
    service_name: str, eventlet_services: Sequence[str] | None
) -> dict[str, str]:
    """Return the eventlet monkey-patch env for ``service_name``, or ``{}``.

    ``{"OTEL_PYTHON_EVENTLET_MONKEY_PATCH": "true"}`` when the service is in
    ``eventlet_services`` (the operator's ``otel_eventlet_services`` list, or
    :data:`DEFAULT_EVENTLET_SERVICES`), else an empty dict. Layered as a
    managed default below the service's own ``environment`` (overridable).
    """
    if service_name in (eventlet_services or []):
        return {EVENTLET_ENV_KEY: "true"}
    return {}


def collector_endpoint_port(
    protocol: str,
    grpc_port: int = DEFAULT_COLLECTOR_GRPC_PORT,
    http_port: int = DEFAULT_COLLECTOR_HTTP_PORT,
) -> int:
    """Return the local collector port for the given exporter protocol.

    Mirrors the port selection in the role's ``otel_local_collector_endpoint``
    default: ``http/protobuf`` targets the collector's HTTP receiver, anything
    else (``grpc``) its gRPC receiver.
    """
    if protocol == "http/protobuf":
        return http_port
    return grpc_port


def address_in_url_context(address: str) -> str:
    """Wrap an IPv6 literal in brackets for a URL authority; else unchanged.

    Mirrors kolla's ``put_address_in_context(addr, 'url')`` filter, which the
    role default applies to the resolved interface address so an IPv6 endpoint
    is well-formed (``http://[fe80::1]:4317``).
    """
    if address and ":" in address and not address.startswith("["):
        return f"[{address}]"
    return address


def interface_address(
    ansible_facts: Mapping[str, Any],
    interface: str,
    address_family: str = "ipv4",
) -> str | None:
    """Return an interface's address from Ansible network facts, or ``None``.

    Mirrors the fact lookup in the role's ``otel_local_collector_endpoint``:
    ``ansible_facts[<interface, '-'->'_'>][<address_family>]['address']``.
    Returns ``None`` when the interface, family or address is absent, so the
    caller can fall back to loopback.
    """
    if not interface:
        return None
    fact = (ansible_facts or {}).get(interface.replace("-", "_")) or {}
    family = fact.get(address_family) or {}
    address = family.get("address")
    return address or None


def local_collector_endpoint(
    address: str | None,
    protocol: str,
    grpc_port: int = DEFAULT_COLLECTOR_GRPC_PORT,
    http_port: int = DEFAULT_COLLECTOR_HTTP_PORT,
) -> str:
    """Build the per-host local-collector OTLP endpoint.

    Mirrors the role's ``otel_local_collector_endpoint`` default:
    ``http://<address>:<port>`` where ``address`` is this host's api_interface
    address (loopback when it cannot be resolved) and the port is chosen from
    ``protocol`` via :func:`collector_endpoint_port`.
    """
    host = address_in_url_context(address or "127.0.0.1")
    port = collector_endpoint_port(protocol, grpc_port, http_port)
    return f"http://{host}:{port}"
