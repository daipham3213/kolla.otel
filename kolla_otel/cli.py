"""The ``kolla-ansible otel-instrument`` / ``otel-rollback`` commands.

Registered under the ``kolla_ansible.cli`` entry-point namespace (see
``pyproject.toml``), these commands follow the same pattern as the built-in
commands in ``kolla_ansible.cli.commands``: they mix in
:class:`~kolla_ansible.cli.commands.KollaAnsibleMixin` and run a playbook via
``run_playbooks``.

* ``otel-instrument`` runs ``otel-instrument.yml`` (the ``otel_instrument``
  role), which injects the opentelemetry-operator auto-instrumentation agent
  and the ``OTEL_*`` environment into the running OpenStack service containers
  (nova-api, nova-conductor, nova-compute, cinder-api, …) and recreates them.
* ``otel-rollback`` runs ``otel-rollback.yml`` — the same role with
  ``otel_action=rollback`` — which strips the injected environment, drops the
  agent bind-mount and the managed-env label, and recreates each affected
  container back to its pre-instrumentation state.
* ``otel-collector`` runs ``otel-collector.yml`` (the ``otel_collector`` role
  on its own) to deploy — or, with ``--remove``, tear down — the per-host
  local collector independently of instrumentation, so it can be stood up and
  verified before services are pointed at it.

Both accept an optional ``--config`` file, validated by
:func:`kolla_otel.config.load_config` and translated into the role's
``otel_*`` extra-vars; without it the role reads its configuration (and, for
rollback, its target service list) from ``globals.yml`` / the role defaults
like any other kolla setting. Passing the same config file used to instrument
is the natural way to roll back exactly what was instrumented.
"""

import argparse
import logging
from pathlib import Path

import yaml
from cliff.command import Command
from kolla_ansible.cli.commands import KollaAnsibleMixin
from kolla_ansible.utils import get_data_files_path

from kolla_otel.config import load_config
from kolla_otel.exceptions import KollaOtelError
from kolla_otel.extravars import to_extra_vars

__all__ = ["Collector", "Instrument", "Rollback"]

_LOG = logging.getLogger(__name__)

#: Playbooks shipped under ``share/kolla-ansible/ansible`` by this package.
INSTRUMENT_PLAYBOOK = "otel-instrument"
ROLLBACK_PLAYBOOK = "otel-rollback"
COLLECTOR_PLAYBOOK = "otel-collector"


class _OtelCommand(KollaAnsibleMixin, Command):
    """Shared base for the OpenTelemetry instrument/rollback commands.

    Subclasses set :attr:`playbook` (the playbook basename shipped under
    ``share/kolla-ansible/ansible``) and :attr:`start_message` (logged before
    the run). Both share the optional ``--config`` handling.
    """

    #: Playbook basename run by this command; set by subclasses.
    playbook: str
    #: Message logged when the command starts; set by subclasses.
    start_message: str

    def get_parser(self, prog_name: str) -> argparse.ArgumentParser:
        """Build the argument parser for the command.

        :param prog_name: Program name supplied by cliff.
        :returns: The configured parser, including the shared Ansible and
            Kolla Ansible argument groups from the mixin.
        """
        parser: argparse.ArgumentParser = super().get_parser(prog_name)
        group = parser.add_argument_group("OpenTelemetry options")
        group.add_argument(
            "--config",
            type=Path,
            metavar="PATH",
            help=(
                "Optional YAML config translated into the role's otel_* "
                "variables. If omitted, configuration is read from "
                "globals.yml."
            ),
        )
        return parser

    def take_action(self, parsed_args: argparse.Namespace) -> int:
        """Run the command's playbook.

        :param parsed_args: Parsed command-line arguments.
        :returns: ``0`` on success (``run_playbooks`` exits non-zero on
            playbook failure).
        :raises KollaOtelError: If the ``--config`` file is missing/invalid.
        """
        self.app.LOG.info(self.start_message)

        extra_vars: dict = dict(self._command_extra_vars(parsed_args))
        if parsed_args.config is not None:
            document = self._load_document(parsed_args.config)
            config, specs = load_config(document)
            extra_vars.update(to_extra_vars(config, specs))
            _LOG.info(
                "Loaded config for %d service(s) from %s",
                len(extra_vars.get("otel_instrument_services", [])),
                parsed_args.config,
            )

        playbooks = [get_data_files_path("ansible", f"{self.playbook}.yml")]
        self.run_playbooks(parsed_args, playbooks, extra_vars=extra_vars)
        return 0

    def _command_extra_vars(
        self, parsed_args: argparse.Namespace
    ) -> dict[str, object]:
        """Return extra-vars a subcommand injects regardless of ``--config``.

        The base command adds none; subcommands override this to pass their
        own switches (e.g. ``otel_action``) through to the playbook. Merged
        beneath any ``--config`` translation.

        :param parsed_args: Parsed command-line arguments.
        :returns: Extra Ansible variables for this command.
        """
        return {}

    @staticmethod
    def _load_document(path: Path) -> dict[str, object]:
        """Load and parse the YAML configuration document.

        :param path: Path to the YAML file.
        :returns: The parsed mapping (empty if the document is blank).
        :raises KollaOtelError: If the file is missing or not valid YAML.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KollaOtelError(
                f"Cannot read configuration file {path}: {exc}"
            ) from exc
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise KollaOtelError(
                f"Invalid YAML in configuration file {path}: {exc}"
            ) from exc
        return loaded if isinstance(loaded, dict) else {}


class Instrument(_OtelCommand):
    """Inject OpenTelemetry auto-instrumentation into Kolla containers"""

    playbook = INSTRUMENT_PLAYBOOK
    start_message = "Injecting OpenTelemetry auto-instrumentation"


class Rollback(_OtelCommand):
    """Remove OpenTelemetry auto-instrumentation from Kolla containers"""

    playbook = ROLLBACK_PLAYBOOK
    start_message = "Rolling back OpenTelemetry auto-instrumentation"


class Collector(_OtelCommand):
    """Deploy (or, with --remove, tear down) the local OpenTelemetry collector

    Runs the ``otel_collector`` role on its own — independently of any
    instrumentation — so the per-host local collector can be stood up (and
    verified) before ``otel-instrument`` points services at it. It only acts
    when no external ``otel_exporter_endpoint`` is configured; otherwise it is
    a no-op.
    """

    playbook = COLLECTOR_PLAYBOOK
    start_message = "Deploying the local OpenTelemetry collector"

    def get_parser(self, prog_name: str) -> argparse.ArgumentParser:
        """Add ``--remove`` to the shared parser.

        :param prog_name: Program name supplied by cliff.
        :returns: The configured parser.
        """
        parser: argparse.ArgumentParser = super().get_parser(prog_name)
        group = parser.add_argument_group("Collector options")
        group.add_argument(
            "--remove",
            action="store_true",
            help=(
                "Stop and remove a collector previously deployed by this "
                "command, instead of deploying one."
            ),
        )
        return parser

    def take_action(self, parsed_args: argparse.Namespace) -> int:
        """Deploy or remove the collector depending on ``--remove``."""
        if parsed_args.remove:
            self.start_message = "Removing the local OpenTelemetry collector"
        return super().take_action(parsed_args)

    def _command_extra_vars(
        self, parsed_args: argparse.Namespace
    ) -> dict[str, object]:
        """Select the role's rollback path when ``--remove`` is given."""
        if parsed_args.remove:
            return {"otel_action": "rollback"}
        return {}
