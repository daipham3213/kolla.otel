"""Tests for :mod:`kolla_otel.localrun` — the self-contained playbook runner
used on kolla-ansible releases without a Python CLI (e.g. 18.8.0)."""

import argparse
import logging
import subprocess
import types

import pytest

from kolla_otel import localrun


class BuildCommandTestCase:
    """`build_playbook_command` mirrors kolla's ansible-playbook invocation."""

    def test_full_command_order_and_content(self) -> None:
        cmd = localrun.build_playbook_command(
            ["/share/kolla-ansible/ansible/otel-instrument.yml"],
            inventory=["/etc/kolla/inventory", "/extra/inv"],
            extra_vars={"otel_action": "rollback", "a": "b"},
            cli_extra_vars=["foo=bar"],
            config_path="/etc/kolla",
            become=True,
            verbose_level=2,
            exists=lambda p: True,  # globals.yml + passwords.yml both present
        )
        assert cmd == [
            "ansible-playbook",
            "-vv",
            "--inventory",
            "/etc/kolla/inventory",
            "--inventory",
            "/extra/inv",
            "-e",
            "@/etc/kolla/globals.yml",
            "-e",
            "@/etc/kolla/passwords.yml",
            "-e",
            "foo=bar",
            "-e",
            '{"a": "b", "otel_action": "rollback"}',  # JSON, sorted keys
            "--become",
            "/share/kolla-ansible/ansible/otel-instrument.yml",
        ]

    def test_minimal_command_omits_absent_files_and_flags(self) -> None:
        cmd = localrun.build_playbook_command(
            ["pb.yml"], exists=lambda p: False
        )
        # No inventory, no globals/passwords, no extra-vars, no become, no -v.
        assert cmd == ["ansible-playbook", "pb.yml"]

    def test_explicit_passwords_path_overrides_default(self) -> None:
        seen = []

        def _exists(path: str) -> bool:
            seen.append(path)
            return path == "/secrets/pw.yml"

        cmd = localrun.build_playbook_command(
            ["pb.yml"],
            config_path="/etc/kolla",
            passwords="/secrets/pw.yml",
            exists=_exists,
        )
        assert "@/secrets/pw.yml" in cmd
        assert "@/etc/kolla/passwords.yml" not in cmd
        # globals.yml is still probed under config_path
        assert "/etc/kolla/globals.yml" in seen


class DataFileResolutionTestCase:
    def test_resolve_returns_first_existing_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cands = localrun.data_file_candidates("ansible", "otel-instrument.yml")
        assert cands  # at least one root probed
        assert all(
            c.endswith("share/kolla-ansible/ansible/otel-instrument.yml")
            for c in cands
        )
        monkeypatch.setattr(
            localrun.os.path, "exists", lambda p: p == cands[-1]
        )
        assert (
            localrun.resolve_data_file("ansible", "otel-instrument.yml")
            == cands[-1]
        )

    def test_resolve_falls_back_to_first_when_none_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(localrun.os.path, "exists", lambda p: False)
        cands = localrun.data_file_candidates("ansible", "x.yml")
        assert localrun.resolve_data_file("ansible", "x.yml") == cands[0]


class AnsibleEnvTestCase:
    def test_sets_ansible_config_when_cfg_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            localrun.os.path,
            "isfile",
            lambda p: p == "/pb/ansible.cfg",
        )
        env = localrun.ansible_env("/pb", base={})
        assert env["ANSIBLE_CONFIG"] == "/pb/ansible.cfg"

    def test_respects_existing_ansible_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(localrun.os.path, "isfile", lambda p: True)
        env = localrun.ansible_env("/pb", base={"ANSIBLE_CONFIG": "/mine"})
        assert env["ANSIBLE_CONFIG"] == "/mine"


class LocalRunnerMixinTestCase:
    """The cliff mixin fallback: parser args + ansible-playbook dispatch."""

    def _command(self):
        cls = type(
            "_Cmd",
            (localrun.LocalRunnerMixin,),
            {"take_action": lambda self, parsed_args: 0},
        )
        app = types.SimpleNamespace(
            options=types.SimpleNamespace(verbose_level=1),
            LOG=logging.getLogger("kolla_otel.tests.localrun"),
        )
        return cls(app=app, app_args=None)

    def test_parser_exposes_kolla_style_args(self) -> None:
        parser = self._command().get_parser("instrument")
        args = parser.parse_args(
            ["-i", "inv1", "-i", "inv2", "--become", "-e", "k=v"]
        )
        assert args.inventory == ["inv1", "inv2"]
        assert args.become is True
        assert args.extra_vars == ["k=v"]
        assert args.configdir == localrun.DEFAULT_CONFIG_PATH

    def test_run_playbooks_builds_and_runs_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        monkeypatch.setattr(
            localrun,
            "run",
            lambda cmd, env=None: captured.update(cmd=cmd, env=env) or 0,
        )
        monkeypatch.setattr(localrun.os.path, "isfile", lambda p: False)
        cmd = self._command()
        parsed = argparse.Namespace(
            inventory=["/etc/kolla/inv"],
            become=True,
            extra_vars=[],
            configdir="/etc/kolla",
            passwords=None,
        )
        cmd.run_playbooks(
            parsed, ["/share/otel-instrument.yml"], extra_vars={"x": "1"}
        )
        assert captured["cmd"][0] == "ansible-playbook"
        assert "--inventory" in captured["cmd"]
        assert "/etc/kolla/inv" in captured["cmd"]
        assert "--become" in captured["cmd"]
        assert captured["cmd"][-1] == "/share/otel-instrument.yml"

    def test_run_playbooks_translates_failure_to_systemexit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(cmd, env=None):
            raise subprocess.CalledProcessError(3, cmd)

        monkeypatch.setattr(localrun, "run", _boom)
        monkeypatch.setattr(localrun.os.path, "isfile", lambda p: False)
        cmd = self._command()
        parsed = argparse.Namespace(
            inventory=[],
            become=False,
            extra_vars=[],
            configdir="/etc/kolla",
            passwords=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd.run_playbooks(parsed, ["pb.yml"])
        assert exc.value.code == 3
