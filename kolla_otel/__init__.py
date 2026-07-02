"""Zero-code OpenTelemetry instrumentation for Kolla Ansible.

``kolla_otel`` extends the ``kolla-ansible`` CLI with an ``instrument``
command that runs the ``otel_instrument`` Ansible role (shipped under
``ansible/``). The role injects the ``opentelemetry-operator``
auto-instrumentation agents into the running OpenStack service containers
and configures the ``OTEL_*`` environment required to export telemetry —
without changing any service code.

The Python package is deliberately small: the injection itself lives in
Ansible. Python provides only

* :mod:`kolla_otel.config` — a validated configuration model and loader.
* :mod:`kolla_otel.extravars` — translation of that model into the role's
  ``otel_*`` variables.
* :mod:`kolla_otel.cli` — the ``cliff`` command that runs the playbook.

Only the stable, high-level building blocks are re-exported here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from kolla_otel.config import (
    InstrumentationLanguage,
    OTelConfig,
    ServiceInstrumentationSpec,
    load_config,
)
from kolla_otel.exceptions import ConfigurationError, KollaOtelError
from kolla_otel.extravars import default_container_name, to_extra_vars

try:
    __version__ = version("kolla-otel")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "__version__",
    # Configuration
    "InstrumentationLanguage",
    "OTelConfig",
    "ServiceInstrumentationSpec",
    "load_config",
    # Ansible bridge
    "to_extra_vars",
    "default_container_name",
    # Exceptions
    "KollaOtelError",
    "ConfigurationError",
]
