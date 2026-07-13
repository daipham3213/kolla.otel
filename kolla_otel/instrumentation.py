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
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "AUGMENT_ACTIONS",
    "LANGUAGE_DEFAULTS",
    "COMMON_ENV_MAP",
    "SCALAR_DEFAULTS",
    "DEFAULT_SERVICES",
    "DEFAULT_HOST_LIB_PATH",
    "DEFAULT_MANAGED_ENV_LABEL",
    "DEFAULT_IMAGE_REGISTRY",
    "DEFAULT_IMAGE_VERSION",
    "deep_merge",
    "resolve_language",
    "resource_attributes",
    "resource_attributes_string",
    "managed_environment",
    "agent_image",
    "stage_paths",
    "agent_bind",
    "apply_agent_mount",
    "managed_label_value",
    "find_service",
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


#: Default target services. Mirrors ``otel_instrument_services`` in the role
#: defaults so the plugin instruments the same containers during a plain
#: ``deploy`` when the operator has not listed them in globals.yml (kept in
#: sync by test_instrumentation.py).
DEFAULT_SERVICES: list[dict[str, str]] = [
    {"name": "nova-api", "container_name": "nova_api", "language": "python"},
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
    {"name": "keystone", "container_name": "keystone", "language": "python"},
    {
        "name": "glance-api",
        "container_name": "glance_api",
        "language": "python",
    },
    {
        "name": "neutron-server",
        "container_name": "neutron_server",
        "language": "python",
    },
    {
        "name": "placement-api",
        "container_name": "placement_api",
        "language": "python",
    },
    {"name": "heat-api", "container_name": "heat_api", "language": "python"},
    {
        "name": "heat-engine",
        "container_name": "heat_engine",
        "language": "python",
    },
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


def apply_agent_mount(
    binds: Sequence[str] | None, mount_path: str, bind: str
) -> list[str]:
    """Return ``binds`` with any existing mount at ``mount_path`` replaced.

    Drops any existing bind whose destination is ``mount_path`` (a mount left
    by an earlier run, named volume or host path) before appending ``bind``,
    so a recreate never hits "Duplicate mount point" — mirrors ``inject.yml``.
    """
    pattern = re.compile(r"^[^:]+:" + re.escape(mount_path) + r"(:.*)?$")
    kept = [b for b in (binds or []) if not pattern.match(b)]
    kept.append(bind)
    return kept


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
