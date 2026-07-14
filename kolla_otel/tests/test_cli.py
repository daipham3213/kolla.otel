"""Tests for :mod:`kolla_otel.cli`."""

import logging

import pytest
import yaml

from kolla_otel import cli as cli_module
from kolla_otel.cli import Instrument, Rollback
from kolla_otel.exceptions import KollaOtelError


class _FakeOptions:
    verbose_level = 1


class _FakeApp:
    """Minimal stand-in for the cliff application object."""

    LOG = logging.getLogger("kolla_otel.tests.cli")
    options = _FakeOptions()


def _command() -> Instrument:
    return Instrument(app=_FakeApp(), app_args=None)


@pytest.fixture
def config_file(tmp_path):
    """Write a valid instrumentation config and return its path."""
    path = tmp_path / "instrumentation.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "otel": {"exporter_endpoint": "http://c:4317"},
                "services": [
                    {"name": "nova-api", "language": "python"},
                    {"name": "cinder-api", "language": "python"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """Patch playbook resolution + execution and record the invocation."""
    calls: dict = {}
    monkeypatch.setattr(
        cli_module, "get_data_files_path", lambda *a: "/share/" + a[-1]
    )

    def _run(cmd: Instrument):
        def _fake(parsed_args, playbooks, extra_vars):
            calls["playbooks"] = playbooks
            calls["extra_vars"] = extra_vars

        monkeypatch.setattr(cmd, "run_playbooks", _fake)

    calls["install"] = _run
    return calls


def test_get_parser_exposes_config_and_ansible_args(config_file) -> None:
    """The parser has --config plus the inherited Ansible arguments."""
    parser = _command().get_parser("instrument")
    args = parser.parse_args(
        ["--config", str(config_file), "--inventory", "multinode"]
    )
    assert args.config == config_file
    assert args.inventory == ["multinode"]  # from KollaAnsibleMixin


def test_get_parser_config_is_optional() -> None:
    """--config may be omitted (configuration then comes from globals.yml)."""
    args = _command().get_parser("instrument").parse_args([])
    assert args.config is None


def test_take_action_runs_playbook_without_config(recorder) -> None:
    """Without --config the playbook runs with empty extra-vars."""
    cmd = _command()
    recorder["install"](cmd)
    args = cmd.get_parser("instrument").parse_args([])

    rc = cmd.take_action(args)

    assert rc == 0
    assert recorder["playbooks"] == ["/share/otel-instrument.yml"]
    assert recorder["extra_vars"] == {}


def test_take_action_translates_config_to_extra_vars(
    recorder, config_file
) -> None:
    """--config is validated and translated into otel_* extra-vars."""
    cmd = _command()
    recorder["install"](cmd)
    args = cmd.get_parser("instrument").parse_args(
        ["--config", str(config_file)]
    )

    rc = cmd.take_action(args)

    assert rc == 0
    extra = recorder["extra_vars"]
    assert extra["otel_exporter_endpoint"] == "http://c:4317"
    names = [s["container_name"] for s in extra["otel_instrument_services"]]
    assert names == ["nova_api", "cinder_api"]


def test_take_action_invalid_config_raises(recorder, tmp_path) -> None:
    """An invalid config document propagates a domain error."""
    bad = tmp_path / "c.yml"
    bad.write_text(yaml.safe_dump({"services": []}), encoding="utf-8")
    cmd = _command()
    recorder["install"](cmd)
    args = cmd.get_parser("instrument").parse_args(["--config", str(bad)])
    with pytest.raises(KollaOtelError):
        cmd.take_action(args)


def _rollback() -> Rollback:
    return Rollback(app=_FakeApp(), app_args=None)


def test_rollback_runs_rollback_playbook_without_config(recorder) -> None:
    """Rollback runs the otel-rollback playbook, not the instrument one."""
    cmd = _rollback()
    recorder["install"](cmd)
    args = cmd.get_parser("rollback").parse_args([])

    rc = cmd.take_action(args)

    assert rc == 0
    assert recorder["playbooks"] == ["/share/otel-rollback.yml"]
    assert recorder["extra_vars"] == {}


def test_rollback_translates_config_to_extra_vars(
    recorder, config_file
) -> None:
    """Rollback takes the same --config to target what was instrumented."""
    cmd = _rollback()
    recorder["install"](cmd)
    args = cmd.get_parser("rollback").parse_args(
        ["--config", str(config_file)]
    )

    rc = cmd.take_action(args)

    assert rc == 0
    names = [
        s["container_name"]
        for s in recorder["extra_vars"]["otel_instrument_services"]
    ]
    assert names == ["nova_api", "cinder_api"]


def test_load_document_reads_yaml(config_file) -> None:
    """A valid YAML file is parsed into a mapping."""
    document = Instrument._load_document(config_file)
    assert document["otel"]["exporter_endpoint"] == "http://c:4317"


def test_load_document_missing_file_raises(tmp_path) -> None:
    """A missing file surfaces a domain error."""
    with pytest.raises(KollaOtelError):
        Instrument._load_document(tmp_path / "nope.yml")


def test_load_document_invalid_yaml_raises(tmp_path) -> None:
    """Malformed YAML surfaces a domain error."""
    bad = tmp_path / "bad.yml"
    bad.write_text("key: : :\n  - broken", encoding="utf-8")
    with pytest.raises(KollaOtelError):
        Instrument._load_document(bad)
