"""Configuration model for zero-code OpenTelemetry instrumentation.

This module defines the immutable, framework-agnostic data structures that
describe *what* should be instrumented and *how* telemetry is exported. It
also provides :func:`load_config`, which validates a plain mapping (for
example the result of parsing a YAML document) into those structures.

The objects here are intentionally free of any third-party dependency so
that the core domain logic can be unit-tested with the standard library
alone.
"""

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from kolla_otel.exceptions import ConfigurationError

__all__ = [
    "InstrumentationLanguage",
    "OTelConfig",
    "ServiceInstrumentationSpec",
    "load_config",
]


class InstrumentationLanguage(enum.Enum):
    """Runtime languages supported by the auto-instrumentation images.

    The values match the language identifiers published by the
    ``opentelemetry-operator`` project (they form part of the image name,
    e.g. ``autoinstrumentation-python``).
    """

    PYTHON = "python"
    JAVA = "java"
    NODEJS = "nodejs"
    DOTNET = "dotnet"

    @classmethod
    def from_string(cls, value: str) -> "InstrumentationLanguage":
        """Return the enum member matching ``value`` (case-insensitive).

        :param value: A language identifier such as ``"python"``.
        :returns: The corresponding :class:`InstrumentationLanguage`.
        :raises ConfigurationError: If ``value`` is not a known language.
        """
        normalised = value.strip().lower()
        # Accept a couple of common aliases for ergonomics.
        aliases = {"node": cls.NODEJS, "js": cls.NODEJS, "net": cls.DOTNET}
        if normalised in aliases:
            return aliases[normalised]
        try:
            return cls(normalised)
        except ValueError as exc:
            supported = ", ".join(member.value for member in cls)
            raise ConfigurationError(
                f"Unsupported instrumentation language {value!r}; "
                f"expected one of: {supported}."
            ) from exc


@dataclass(frozen=True)
class OTelConfig:
    """Deployment-wide OpenTelemetry export and image settings.

    :param exporter_endpoint: OTLP collector endpoint
        (e.g. ``http://otel-collector:4317``).
    :param exporter_protocol: OTLP transport, ``"grpc"`` or
        ``"http/protobuf"``.
    :param image_registry: Registry/repository prefix that hosts the
        ``opentelemetry-operator`` auto-instrumentation images.
    :param image_version: Image tag to pin (``"latest"`` by default).
    :param traces_exporter: Value for ``OTEL_TRACES_EXPORTER``.
    :param metrics_exporter: Value for ``OTEL_METRICS_EXPORTER``.
    :param logs_exporter: Value for ``OTEL_LOGS_EXPORTER``.
    :param sampler: Value for ``OTEL_TRACES_SAMPLER``.
    :param sampler_arg: Value for ``OTEL_TRACES_SAMPLER_ARG``.
    :param propagators: Context propagators (``OTEL_PROPAGATORS``).
    :param service_namespace: ``service.namespace`` resource attribute
        shared by every instrumented service.
    :param deployment_environment: Optional ``deployment.environment``
        resource attribute (e.g. ``"production"``).
    :param resource_attributes: Extra resource attributes applied to every
        service, merged beneath per-service attributes.
    """

    exporter_endpoint: str
    exporter_protocol: str = "grpc"
    image_registry: str = "ghcr.io/open-telemetry/opentelemetry-operator"
    image_version: str = "latest"
    traces_exporter: str = "otlp"
    metrics_exporter: str = "otlp"
    logs_exporter: str = "otlp"
    sampler: str = "parentbased_traceidratio"
    sampler_arg: str = "1.0"
    propagators: tuple[str, ...] = ("tracecontext", "baggage")
    service_namespace: str = "openstack"
    deployment_environment: str | None = None
    resource_attributes: Mapping[str, str] = field(default_factory=dict)

    _VALID_PROTOCOLS = ("grpc", "http/protobuf")

    def __post_init__(self) -> None:
        if not self.exporter_endpoint:
            raise ConfigurationError("'exporter_endpoint' must not be empty.")
        if self.exporter_protocol not in self._VALID_PROTOCOLS:
            raise ConfigurationError(
                f"'exporter_protocol' must be one of "
                f"{self._VALID_PROTOCOLS}, got {self.exporter_protocol!r}."
            )


@dataclass(frozen=True)
class ServiceInstrumentationSpec:
    """A single Kolla service selected for instrumentation.

    :param name: Kolla service/container name (e.g. ``"nova-api"``).
    :param language: Runtime language of the service.
    :param otel_service_name: Value for ``OTEL_SERVICE_NAME``. Defaults to
        :attr:`name` when omitted.
    :param resource_attributes: Per-service resource attributes, taking
        precedence over the deployment-wide attributes.
    :param environment: Extra environment variables for just this service,
        layered above the common ``OTEL_*`` env (below language activation).
    :param enabled: When ``False`` the service is skipped during
        instrumentation.
    """

    name: str
    language: InstrumentationLanguage
    otel_service_name: str | None = None
    resource_attributes: Mapping[str, str] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ConfigurationError("Service 'name' must not be empty.")

    @property
    def service_name(self) -> str:
        """Return the effective ``OTEL_SERVICE_NAME`` for this service."""
        return self.otel_service_name or self.name


def _as_str_mapping(value: object, context: str) -> dict[str, str]:
    """Coerce ``value`` into a ``dict[str, str]`` or raise.

    :param value: The object to validate (expected to be a mapping).
    :param context: Human-readable location used in error messages.
    :raises ConfigurationError: If ``value`` is not a string-keyed mapping.
    """
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{context} must be a mapping.")
    return {str(key): str(item) for key, item in value.items()}


def load_config(
    document: Mapping[str, object],
) -> tuple[OTelConfig, list[ServiceInstrumentationSpec]]:
    """Validate a raw mapping into an :class:`OTelConfig` and specs.

    The expected document shape is::

        otel:
          exporter_endpoint: http://otel-collector:4317
          exporter_protocol: grpc
          deployment_environment: production
        services:
          - name: nova-api
            language: python
          - name: keystone
            language: python
            otel_service_name: identity

    :param document: Parsed configuration document (e.g. from YAML/JSON).
    :returns: A tuple of the validated :class:`OTelConfig` and the list of
        :class:`ServiceInstrumentationSpec` objects.
    :raises ConfigurationError: If required keys are missing or any value
        has an unexpected type.
    """
    if not isinstance(document, Mapping):
        raise ConfigurationError("Top-level configuration must be a mapping.")

    otel_section = document.get("otel")
    if not isinstance(otel_section, Mapping):
        raise ConfigurationError("Missing required 'otel' mapping section.")

    endpoint = otel_section.get("exporter_endpoint")
    if not isinstance(endpoint, str):
        raise ConfigurationError(
            "'otel.exporter_endpoint' is required and must be a string."
        )

    propagators_raw = otel_section.get("propagators")
    if propagators_raw is None:
        propagators: tuple[str, ...] = OTelConfig.propagators
    elif isinstance(propagators_raw, Sequence) and not isinstance(
        propagators_raw, (str, bytes)
    ):
        propagators = tuple(str(item) for item in propagators_raw)
    else:
        raise ConfigurationError("'otel.propagators' must be a list.")

    # Build kwargs only for keys the user actually provided, letting the
    # dataclass defaults cover the rest. Unknown keys are rejected to catch
    # typos early rather than silently ignoring them.
    known = {
        "exporter_endpoint",
        "exporter_protocol",
        "image_registry",
        "image_version",
        "traces_exporter",
        "metrics_exporter",
        "logs_exporter",
        "sampler",
        "sampler_arg",
        "propagators",
        "service_namespace",
        "deployment_environment",
        "resource_attributes",
    }
    unknown = set(otel_section) - known
    if unknown:
        raise ConfigurationError(
            f"Unknown key(s) in 'otel' section: {', '.join(sorted(unknown))}."
        )

    config = OTelConfig(
        exporter_endpoint=endpoint,
        exporter_protocol=str(otel_section.get("exporter_protocol", "grpc")),
        image_registry=str(
            otel_section.get(
                "image_registry",
                "ghcr.io/open-telemetry/opentelemetry-operator",
            )
        ),
        image_version=str(otel_section.get("image_version", "latest")),
        traces_exporter=str(otel_section.get("traces_exporter", "otlp")),
        metrics_exporter=str(otel_section.get("metrics_exporter", "otlp")),
        logs_exporter=str(otel_section.get("logs_exporter", "otlp")),
        sampler=str(otel_section.get("sampler", "parentbased_traceidratio")),
        sampler_arg=str(otel_section.get("sampler_arg", "1.0")),
        propagators=propagators,
        service_namespace=str(
            otel_section.get("service_namespace", "openstack")
        ),
        deployment_environment=(
            None
            if otel_section.get("deployment_environment") is None
            else str(otel_section["deployment_environment"])
        ),
        resource_attributes=_as_str_mapping(
            otel_section.get("resource_attributes", {}),
            "'otel.resource_attributes'",
        ),
    )

    services_raw = document.get("services")
    if not isinstance(services_raw, Sequence) or isinstance(
        services_raw, (str, bytes)
    ):
        raise ConfigurationError("'services' must be a non-empty list.")
    if not services_raw:
        raise ConfigurationError("'services' must contain at least one entry.")

    specs: list[ServiceInstrumentationSpec] = []
    for index, entry in enumerate(services_raw):
        if not isinstance(entry, Mapping):
            raise ConfigurationError(f"services[{index}] must be a mapping.")
        name = entry.get("name")
        if not isinstance(name, str):
            raise ConfigurationError(
                f"services[{index}].name is required and must be a string."
            )
        language = entry.get("language")
        if not isinstance(language, str):
            raise ConfigurationError(
                f"services[{index}].language is required and must be a string."
            )
        otel_service_name = entry.get("otel_service_name")
        if otel_service_name is not None and not isinstance(
            otel_service_name, str
        ):
            raise ConfigurationError(
                f"services[{index}].otel_service_name must be a string."
            )
        specs.append(
            ServiceInstrumentationSpec(
                name=name,
                language=InstrumentationLanguage.from_string(language),
                otel_service_name=otel_service_name,
                resource_attributes=_as_str_mapping(
                    entry.get("resource_attributes", {}),
                    f"services[{index}].resource_attributes",
                ),
                environment=_as_str_mapping(
                    entry.get("environment", {}),
                    f"services[{index}].environment",
                ),
                enabled=bool(entry.get("enabled", True)),
            )
        )

    return config, specs
