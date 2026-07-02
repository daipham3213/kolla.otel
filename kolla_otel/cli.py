"""The ``kolla-ansible instrument`` command.

Registered under the ``kolla_ansible.cli`` entry-point namespace as
``instrument`` (see ``pyproject.toml``), this command follows the same
pattern as the built-in commands in ``kolla_ansible.cli.commands``: it
mixes in :class:`~kolla_ansible.cli.commands.KollaAnsibleMixin` and runs a
playbook via ``run_playbooks``.

The playbook it runs — ``otel-instrument.yml`` (the ``otel_instrument``
role) — injects the opentelemetry-operator auto-instrumentation agent and
the ``OTEL_*`` environment into the running OpenStack service containers
(nova-api, nova-conductor, nova-compute, cinder-api, …) and recreates them.

An optional ``--config`` file is validated by
:func:`kolla_otel.config.load_config` and translated into the role's
``otel_*`` extra-vars; without it the role
reads its configuration from ``globals.yml`` like any other kolla setting.
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

__all__ = ["Instrument"]

_LOG = logging.getLogger(__name__)

#: Playbook shipped under ``share/kolla-ansible/ansible`` by this package.
PLAYBOOK = "otel-instrument"


class Instrument(KollaAnsibleMixin, Command):
    """Inject OpenTelemetry auto-instrumentation into Kolla containers"""

    def get_parser(self, prog_name: str) -> argparse.ArgumentParser:
        """Build the argument parser for the command.

        :param prog_name: Program name supplied by cliff.
        :returns: The configured parser, including the shared Ansible and
            Kolla Ansible argument groups from the mixin.
        """
        parser: argparse.ArgumentParser = super().get_parser(prog_name)
        group = parser.add_argument_group("OpenTelemetry instrument options")
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
        """Run the instrumentation playbook.

        :param parsed_args: Parsed command-line arguments.
        :returns: ``0`` on success (``run_playbooks`` exits non-zero on
            playbook failure).
        :raises KollaOtelError: If the ``--config`` file is missing/invalid.
        """
        self.app.LOG.info("Injecting OpenTelemetry auto-instrumentation")

        extra_vars: dict = {}
        if parsed_args.config is not None:
            document = self._load_document(parsed_args.config)
            config, specs = load_config(document)
            extra_vars = to_extra_vars(config, specs)
            _LOG.info(
                "Loaded instrumentation config for %d service(s) from %s",
                len(extra_vars.get("otel_instrument_services", [])),
                parsed_args.config,
            )

        playbooks = [get_data_files_path("ansible", f"{PLAYBOOK}.yml")]
        self.run_playbooks(parsed_args, playbooks, extra_vars=extra_vars)
        return 0

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
