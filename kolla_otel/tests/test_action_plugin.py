"""Tests for the bundled ``kolla_container`` action plugin.

The plugin ships under ``ansible/action_plugins/`` (installed adjacent to
kolla's ``site.yml``), so it is loaded here by path. These tests exercise the
safety-critical behaviour: it is off by default, only augments create actions
for targeted containers, and fails open (never raises out of ``run``).
"""

import importlib.util
import types
from pathlib import Path

import pytest
from ansible.plugins.action import ActionBase

_PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "ansible"
    / "action_plugins"
    / "kolla_container.py"
)


def _load_plugin_class():
    spec = importlib.util.spec_from_file_location(
        "otel_kolla_container_action", _PLUGIN_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ActionModule


ActionModule = _load_plugin_class()


class _Templar:
    """Identity templar (test values contain no Jinja)."""

    def template(self, value, fail_on_undefined=True):
        return value


def _plugin(task_args, executed_sink):
    """Build an ActionModule instance wired with test doubles."""
    plugin = ActionModule.__new__(ActionModule)
    plugin._task = types.SimpleNamespace(args=dict(task_args))
    plugin._templar = _Templar()

    def _execute_module(module_name, module_args, task_vars):
        executed_sink["module_name"] = module_name
        executed_sink["args"] = module_args
        return {"changed": False}

    plugin._execute_module = _execute_module
    return plugin


@pytest.fixture(autouse=True)
def _stub_base_run(monkeypatch):
    """ActionBase.run does real setup we don't need; stub it to ``{}``."""
    monkeypatch.setattr(
        ActionBase, "run", lambda self, tmp=None, task_vars=None: {}
    )


_TARGET_ARGS = {
    "action": "recreate_or_restart_container",
    "name": "nova_api",
    "environment": {"KOLLA_X": "1"},
    "volumes": ["/etc/kolla/nova:/var/lib/kolla/config_files:ro"],
    "labels": {"kolla_version": "22"},
}
_ENABLED = {
    "otel_auto_instrument": True,
    "otel_exporter_endpoint": "http://collector:4317",
}


def test_disabled_by_default_is_passthrough():
    sink = {}
    _plugin(_TARGET_ARGS, sink).run(task_vars={})
    # No opt-in -> args delegated unchanged.
    assert sink["args"] == _TARGET_ARGS
    assert "kolla_otel.managed_env" not in sink["args"]["labels"]


def test_enabled_target_container_is_instrumented():
    sink = {}
    _plugin(_TARGET_ARGS, sink).run(task_vars=dict(_ENABLED))
    args = sink["args"]
    env = args["environment"]
    assert env["KOLLA_X"] == "1"  # kolla's own env preserved
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4317"
    assert env["OTEL_SERVICE_NAME"] == "nova-api"
    assert "PYTHONPATH" in env  # language activation applied
    # agent mount appended, config mount preserved
    assert (
        "/etc/kolla/opentelemetry/python:"
        "/otel-auto-instrumentation-python:ro" in args["volumes"]
    )
    assert "/etc/kolla/nova:/var/lib/kolla/config_files:ro" in args["volumes"]
    # managed-env label records the injected keys
    assert "kolla_otel.managed_env" in args["labels"]
    assert args["labels"]["kolla_version"] == "22"


def test_enabled_but_endpoint_missing_is_passthrough():
    sink = {}
    _plugin(_TARGET_ARGS, sink).run(task_vars={"otel_auto_instrument": True})
    assert "kolla_otel.managed_env" not in sink["args"]["labels"]


def test_enabled_non_target_container_is_passthrough():
    sink = {}
    args = dict(_TARGET_ARGS, name="rabbitmq")
    _plugin(args, sink).run(task_vars=dict(_ENABLED))
    assert "kolla_otel.managed_env" not in sink["args"]["labels"]


def test_non_augmentable_action_is_passthrough():
    sink = {}
    args = dict(_TARGET_ARGS, action="remove_container")
    _plugin(args, sink).run(task_vars=dict(_ENABLED))
    assert "kolla_otel.managed_env" not in sink["args"]["labels"]


def test_compare_container_is_made_otel_aware():
    """compare_container gets the same overlay so kolla detects the diff and
    fires its recreate handler (which is then augmented too)."""
    sink = {}
    args = {
        "action": "compare_container",
        "name": "nova_api",
        "environment": {"KOLLA_X": "1"},
    }
    _plugin(args, sink).run(task_vars=dict(_ENABLED))
    augmented = sink["args"]
    # The compared desired spec now includes the OTEL env/mount/label, so a
    # not-yet-instrumented running container will compare as different.
    assert augmented["environment"]["OTEL_SERVICE_NAME"] == "nova-api"
    assert "PYTHONPATH" in augmented["environment"]
    assert any(
        "/otel-auto-instrumentation-python:ro" in v
        for v in augmented["volumes"]
    )
    assert "kolla_otel.managed_env" in augmented["labels"]


def test_custom_service_list_is_honored():
    sink = {}
    task_vars = dict(
        _ENABLED,
        otel_instrument_services=[
            {"name": "svc", "container_name": "my_svc", "language": "java"}
        ],
    )
    args = dict(_TARGET_ARGS, name="my_svc")
    _plugin(args, sink).run(task_vars=task_vars)
    env = sink["args"]["environment"]
    assert "JAVA_TOOL_OPTIONS" in env  # java activation
    assert env["OTEL_SERVICE_NAME"] == "svc"


def test_fails_open_on_error(monkeypatch):
    """Any error while instrumenting must delegate unmodified, not raise."""
    sink = {}
    plugin = _plugin(_TARGET_ARGS, sink)

    def _boom(module_args, task_vars):
        raise RuntimeError("kaboom")

    plugin._maybe_instrument = _boom
    # Should not raise, and should delegate the original args.
    result = plugin.run(task_vars=dict(_ENABLED))
    assert result == {"changed": False}
    assert sink["args"] == _TARGET_ARGS
