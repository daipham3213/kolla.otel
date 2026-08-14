# Copyright 2024 kolla-otel
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Self-contained playbook execution for kolla-ansible without its Python CLI.

Older kolla-ansible releases — notably 18.8.0 — ship no ``kolla_ansible.cli``
(cliff) package and no ``kolla_ansible.utils``; ``kolla-ansible`` is the bash
entry point there. So :class:`~kolla_ansible.cli.commands.KollaAnsibleMixin`
and ``run_playbooks`` (which the ``kolla-otel`` commands normally mix in) are
absent. This module provides a drop-in fallback that runs the same playbook by
invoking ``ansible-playbook`` directly with the standard kolla arguments
(inventory, ``-e @globals.yml``, ``-e @passwords.yml``, extra-vars, become),
so ``kolla-otel`` works regardless of the kolla-ansible CLI version.

The argument-building and data-file resolution are pure functions so they can
be unit-tested without a subprocess; :class:`LocalRunnerMixin` wires them into
a cliff command.
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Mapping, Sequence

from cliff.command import Command

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "data_file_candidates",
    "resolve_data_file",
    "build_playbook_command",
    "ansible_env",
    "run",
    "LocalRunnerMixin",
]

#: Where kolla keeps globals.yml / passwords.yml by default.
DEFAULT_CONFIG_PATH = "/etc/kolla"


def data_file_candidates(*parts: str) -> list[str]:
    """Return candidate absolute paths for a shipped data file.

    Package data (playbooks, roles, action plugins) is installed under
    ``<root>/share/kolla-ansible/...`` for several possible roots. We probe the
    wheel ``data`` scheme first (covers virtualenvs and custom prefixes), then
    common system prefixes, mirroring how kolla's own ``get_data_files_path``
    searches. Order is preserved and duplicates removed.
    """
    roots: list[str] = []
    with contextlib.suppress(KeyError, OSError):  # scheme is always present
        roots.append(sysconfig.get_path("data"))
    roots += [
        sys.prefix,
        getattr(sys, "base_prefix", sys.prefix),
        "/usr/local",
        "/usr",
    ]
    seen: set[str] = set()
    candidates: list[str] = []
    for root in roots:
        if root and root not in seen:
            seen.add(root)
            candidates.append(
                os.path.join(root, "share", "kolla-ansible", *parts)
            )
    return candidates


def resolve_data_file(*parts: str) -> str:
    """Return the first existing candidate for a data file, else the first.

    Drop-in replacement for kolla's ``get_data_files_path``. When nothing
    exists yet the first candidate is returned so the caller surfaces a clear
    "playbook not found" error at run time rather than here.
    """
    candidates = data_file_candidates(*parts)
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0] if candidates else os.path.join(*parts)


def build_playbook_command(
    playbooks: Sequence[str],
    *,
    inventory: Sequence[str] = (),
    extra_vars: Mapping[str, object] | None = None,
    cli_extra_vars: Sequence[str] = (),
    config_path: str = DEFAULT_CONFIG_PATH,
    passwords: str | None = None,
    become: bool = False,
    verbose_level: int = 0,
    exists: Callable[[str], bool] = os.path.isfile,
) -> list[str]:
    """Build the ``ansible-playbook`` argv, mirroring kolla's own runner.

    Loads ``globals.yml`` and ``passwords.yml`` from ``config_path`` (when they
    exist) as extra-var files, appends any raw ``cli_extra_vars`` and the JSON
    ``extra_vars`` mapping, and adds ``--become`` when requested — matching the
    order kolla-ansible uses. ``exists`` is injectable for testing.
    """
    command: list[str] = ["ansible-playbook"]
    if verbose_level > 0:
        command.append("-" + "v" * verbose_level)
    for inv in inventory:
        command += ["--inventory", inv]

    globals_file = os.path.join(config_path, "globals.yml")
    if exists(globals_file):
        command += ["-e", "@" + globals_file]
    passwords_file = passwords or os.path.join(config_path, "passwords.yml")
    if exists(passwords_file):
        command += ["-e", "@" + passwords_file]

    for raw in cli_extra_vars:
        command += ["-e", raw]
    if extra_vars:
        command += ["-e", json.dumps(dict(extra_vars), sort_keys=True)]

    if become:
        command.append("--become")

    command += list(playbooks)
    return command


def ansible_env(
    playbook_dir: str, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the environment for the run, pointing at kolla's ``ansible.cfg``.

    kolla ships an ``ansible.cfg`` next to its playbooks; honoring it keeps
    forks, fact-gathering and plugin paths consistent with a normal kolla run.
    An ``ANSIBLE_CONFIG`` already set by the operator wins.
    """
    env = dict(os.environ if base is None else base)
    config = os.path.join(playbook_dir, "ansible.cfg")
    if "ANSIBLE_CONFIG" not in env and os.path.isfile(config):
        env["ANSIBLE_CONFIG"] = config
    return env


def run(cmd: Sequence[str], env: Mapping[str, str] | None = None) -> int:
    """Run ``cmd`` to completion, returning its exit code (0 on success).

    Raises :class:`subprocess.CalledProcessError` on a non-zero exit so the
    caller can translate it into a process exit code.
    """
    completed = subprocess.run(
        list(cmd), env=None if env is None else dict(env), check=True
    )
    return completed.returncode


class LocalRunnerMixin(Command):
    """Fallback for :class:`~kolla_ansible.cli.commands.KollaAnsibleMixin`.

    Adds the kolla-style Ansible arguments and runs the playbook via
    ``ansible-playbook`` directly, for kolla-ansible releases with no Python
    CLI. Exposes the same surface the ``kolla-otel`` commands rely on
    (``get_parser`` adding the args, and ``run_playbooks``).
    """

    def get_parser(self, prog_name: str) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = super().get_parser(prog_name)
        ansible = parser.add_argument_group("Ansible arguments")
        ansible.add_argument(
            "-i",
            "--inventory",
            action="append",
            default=[],
            metavar="PATH",
            help="Ansible inventory path (repeatable).",
        )
        ansible.add_argument(
            "--become",
            action="store_true",
            help="run operations with become (root).",
        )
        ansible.add_argument(
            "-e",
            "--extra-vars",
            dest="extra_vars",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="extra Ansible variables (repeatable).",
        )
        kolla = parser.add_argument_group("Kolla Ansible arguments")
        kolla.add_argument(
            "--configdir",
            "--kolla-config-path",
            dest="configdir",
            default=DEFAULT_CONFIG_PATH,
            metavar="PATH",
            help=(
                "kolla config dir holding globals.yml / passwords.yml "
                f"(default {DEFAULT_CONFIG_PATH})."
            ),
        )
        kolla.add_argument(
            "--passwords",
            default=None,
            metavar="PATH",
            help="passwords.yml path (default <configdir>/passwords.yml).",
        )
        return parser

    def run_playbooks(
        self,
        parsed_args: argparse.Namespace,
        playbooks: Sequence[str],
        extra_vars: Mapping[str, object] | None = None,
    ) -> None:
        # cliff verbosity is 1-based (1 = normal); ansible -v starts above it.
        verbose = max(
            (getattr(self.app.options, "verbose_level", 1) or 1) - 1, 0
        )
        command = build_playbook_command(
            playbooks,
            inventory=getattr(parsed_args, "inventory", []) or [],
            extra_vars=extra_vars or {},
            cli_extra_vars=getattr(parsed_args, "extra_vars", []) or [],
            config_path=getattr(parsed_args, "configdir", DEFAULT_CONFIG_PATH),
            passwords=getattr(parsed_args, "passwords", None),
            become=bool(getattr(parsed_args, "become", False)),
            verbose_level=verbose,
        )
        env = ansible_env(os.path.dirname(playbooks[0]) if playbooks else "")
        try:
            run(command, env=env)
        except subprocess.CalledProcessError as exc:
            self.app.LOG.error(
                "Playbook(s) %s exited %d",
                ", ".join(playbooks),
                exc.returncode,
            )
            raise SystemExit(exc.returncode) from exc
