"""Tests for :mod:`kolla_otel.app`."""

from cliff.app import App

from kolla_otel import app as app_module
from kolla_otel.app import COMMANDS, KollaOtelApp, main
from kolla_otel.cli import Collector, Instrument, Rollback


class KollaOtelAppTestCase:
    """The standalone ``kolla-otel`` cliff application."""

    def test_is_a_cliff_app(self) -> None:
        """The app is a cliff application."""
        assert issubclass(KollaOtelApp, App)

    def test_commands_map_to_command_classes(self) -> None:
        """The exposed commands map the CLI names to the command classes."""
        assert {
            "instrument": Instrument,
            "rollback": Rollback,
            "collector": Collector,
        } == COMMANDS

    def test_registers_every_command(self) -> None:
        """Each command is dispatchable without entry-point discovery."""
        app = KollaOtelApp()
        for name, command in COMMANDS.items():
            found = app.command_manager.find_command([name])[0]
            assert found is command

    def test_version_is_a_string(self) -> None:
        """The reported version resolves to a string."""
        assert isinstance(app_module._version(), str)

    def test_version_falls_back_when_unknown(self, monkeypatch) -> None:
        """An undeterminable version degrades to ``0.0.0``."""

        def _raise(_name):
            raise app_module.metadata.PackageNotFoundError("kolla-otel")

        monkeypatch.setattr(app_module.metadata, "version", _raise)
        assert app_module._version() == "0.0.0"


class MainTestCase:
    """The ``main`` console-script entry point."""

    def test_passes_explicit_argv_to_app(self, monkeypatch) -> None:
        """An explicit argv is forwarded verbatim to the app."""
        captured = {}

        def _run(self, argv):
            captured["argv"] = argv
            return 0

        monkeypatch.setattr(KollaOtelApp, "run", _run)
        assert main(["instrument", "--help"]) == 0
        assert captured["argv"] == ["instrument", "--help"]

    def test_defaults_argv_to_sys_argv(self, monkeypatch) -> None:
        """With no argv, main uses ``sys.argv[1:]``."""
        captured = {}

        def _run(self, argv):
            captured["argv"] = argv
            return 0

        monkeypatch.setattr(KollaOtelApp, "run", _run)
        monkeypatch.setattr(app_module.sys, "argv", ["kolla-otel", "rollback"])
        main()
        assert captured["argv"] == ["rollback"]
