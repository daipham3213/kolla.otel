"""Standalone ``kolla-otel`` command-line application.

kolla-otel's commands are normally discovered by the ``kolla-ansible`` CLI via
the ``kolla_ansible.cli`` entry-point namespace (see ``pyproject.toml``).
Older kolla-ansible releases — e.g. 18.8.0 — do **not** load external commands
from that namespace, so on those deployments ``kolla-ansible otel-instrument``
is not available.

This module provides a self-contained :mod:`cliff` application, installed as
the ``kolla-otel`` console script, that hosts the same commands directly and
so works regardless of the kolla-ansible CLI version::

    kolla-otel instrument -i /etc/kolla/inventory
    kolla-otel rollback   -i /etc/kolla/inventory
    kolla-otel collector  -i /etc/kolla/inventory [--remove]

The commands themselves are unchanged (:mod:`kolla_otel.cli`); they still mix
in kolla-ansible's ``KollaAnsibleMixin`` and run the same playbooks, so a
working kolla-ansible install is still required — only the command *dispatch*
is made independent of the kolla-ansible CLI. Commands are registered
imperatively (not via an entry-point namespace) so dispatch never depends on
entry-point discovery working.
"""

import sys
from importlib import metadata

from cliff.app import App
from cliff.command import Command
from cliff.commandmanager import CommandManager

from kolla_otel.cli import Collector, Instrument, Rollback

__all__ = ["COMMANDS", "KollaOtelApp", "main"]

#: cliff command namespace for the standalone app. Cosmetic only: commands are
#: registered imperatively below rather than discovered from this namespace.
COMMAND_NAMESPACE = "kolla_otel.cli"

#: Commands the standalone app exposes, keyed by the name typed on the CLI.
COMMANDS: dict[str, type[Command]] = {
    "instrument": Instrument,
    "rollback": Rollback,
    "collector": Collector,
}


def _version() -> str:
    """Return the installed package version (``0.0.0`` if undeterminable)."""
    try:
        return metadata.version("kolla-otel")
    except metadata.PackageNotFoundError:
        return "0.0.0"


class KollaOtelApp(App):
    """A self-contained cliff app hosting the kolla-otel commands.

    Commands are registered imperatively via
    :meth:`~cliff.commandmanager.CommandManager.add_command`, so the app works
    even where entry-point discovery for the ``kolla_otel.cli`` namespace is
    unavailable (the reason this standalone app exists).
    """

    def __init__(self) -> None:
        super().__init__(
            description=(
                "kolla-otel: OpenTelemetry auto-instrumentation for "
                "Kolla Ansible"
            ),
            version=_version(),
            command_manager=CommandManager(COMMAND_NAMESPACE),
            deferred_help=True,
        )
        for name, command in COMMANDS.items():
            self.command_manager.add_command(name, command)

    def initialize_app(self, argv: list[str]) -> None:
        self.LOG.debug("initialize_app")

    def prepare_to_run_command(self, cmd: Command) -> None:
        self.LOG.debug("prepare_to_run_command %s", cmd.__class__.__name__)

    def clean_up(
        self, cmd: Command, result: int, err: BaseException | None
    ) -> None:
        self.LOG.debug("clean_up %s", cmd.__class__.__name__)
        if err:
            self.LOG.debug("got an error: %s", err)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point for ``kolla-otel``.

    :param argv: Argument list (defaults to ``sys.argv[1:]``).
    :returns: The process exit code from the cliff application.
    """
    argv = argv or sys.argv[1:]
    app = KollaOtelApp()
    return app.run(argv)  # type: ignore


if __name__ == "__main__":
    sys.exit(main())
