"""Translate the validated config model into Ansible extra-vars.

The actual injection is performed by the ``otel_instrument`` Ansible role
(shipped under ``ansible/``). This module bridges the typed configuration
model (:mod:`kolla_otel.config`) to the ``otel_*`` variables that role
consumes, so the CLI can validate a user's config file and hand it to the
playbook — mirroring how ``kolla_ansible.cli.commands`` pass ``extra_vars``
to ``run_playbooks``.
"""

from collections.abc import Sequence
from typing import Any

from kolla_otel.config import OTelConfig, ServiceInstrumentationSpec

__all__ = ["default_container_name", "to_extra_vars"]


def default_container_name(service_name: str) -> str:
    """Return the kolla container name for a service name.

    Kolla names containers with underscores (``nova_api``) while service
    names conventionally use hyphens (``nova-api``).

    :param service_name: The hyphenated service name.
    :returns: The underscored container name.
    """
    return service_name.replace("-", "_")


def to_extra_vars(
    config: OTelConfig,
    specs: Sequence[ServiceInstrumentationSpec],
) -> dict[str, Any]:
    """Render ``config``/``specs`` into the role's ``otel_*`` variables.

    :param config: Validated deployment-wide OpenTelemetry configuration.
    :param specs: The services to instrument (disabled specs are dropped).
    :returns: A mapping suitable for ``run_playbooks(extra_vars=...)`` and
        consumed by the ``otel_instrument`` role's defaults.
    """
    extra_vars: dict[str, Any] = {
        "otel_exporter_endpoint": config.exporter_endpoint,
        "otel_exporter_protocol": config.exporter_protocol,
        "otel_traces_exporter": config.traces_exporter,
        "otel_metrics_exporter": config.metrics_exporter,
        "otel_logs_exporter": config.logs_exporter,
        "otel_traces_sampler": config.sampler,
        "otel_traces_sampler_arg": config.sampler_arg,
        "otel_propagators": ",".join(config.propagators),
        "otel_service_namespace": config.service_namespace,
        "otel_image_registry": config.image_registry,
        "otel_image_version": config.image_version,
    }
    if config.deployment_environment is not None:
        extra_vars["otel_deployment_environment"] = (
            config.deployment_environment
        )
    if config.resource_attributes:
        extra_vars["otel_resource_attributes_extra"] = dict(
            config.resource_attributes
        )

    services: list[dict[str, Any]] = []
    for spec in specs:
        if not spec.enabled:
            continue
        entry: dict[str, Any] = {
            "name": spec.name,
            "container_name": default_container_name(spec.name),
            "language": spec.language.value,
        }
        if spec.otel_service_name is not None:
            entry["otel_service_name"] = spec.otel_service_name
        if spec.resource_attributes:
            entry["resource_attributes"] = dict(spec.resource_attributes)
        services.append(entry)
    extra_vars["otel_instrument_services"] = services

    return extra_vars
