"""Tests for the bundled ``kolla_container`` action plugin.

The plugin ships under ``ansible/action_plugins/`` (installed adjacent to
kolla's ``site.yml``), so it is loaded here by path. These tests exercise the
safety-critical behaviour: it is off by default, only augments create actions
for targeted containers, and fails open (never raises out of ``run``).
"""

import importlib.util
import json
import types
from pathlib import Path

import pytest
from ansible.plugins.action import ActionBase

_PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "ansible"
    / "action_plugins"
    / "kolla_container.py"
)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "otel_kolla_container_action", _PLUGIN_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PLUGIN_MOD = _load_plugin_module()
ActionModule = _PLUGIN_MOD.ActionModule


class _Templar:
    """Identity templar (test values contain no Jinja)."""

    def template(self, value, fail_on_undefined=True):
        return value


def _staging_result(module_name, module_args, stage_ok):
    """Canned result for a host-side staging module call."""
    if module_name == "command":
        argv = module_args.get("argv", [])
        if "inspect" in argv:
            if not stage_ok:
                return {"failed": True, "rc": 1}
            return {"rc": 0, "stdout": json.dumps([{"Id": "sha256:test"}])}
        return {"rc": 0}  # pull / run cp
    if module_name == "stat":
        return {"stat": {"exists": False}}
    if module_name == "slurp":
        return {"failed": True}  # marker absent -> triggers copy
    return {"changed": True}  # file / copy


def _plugin(task_args, executed_sink, stage_ok=True):
    """Build an ActionModule instance wired with test doubles."""
    plugin = ActionModule.__new__(ActionModule)
    plugin._task = types.SimpleNamespace(
        args=dict(task_args), check_mode=False
    )
    plugin._templar = _Templar()

    def _execute_module(module_name, module_args, task_vars):
        if module_name == "kolla_container":  # the delegated (final) call
            executed_sink["module_name"] = module_name
            executed_sink["args"] = module_args
            return {"changed": False}
        return _staging_result(module_name, module_args, stage_ok)

    plugin._execute_module = _execute_module
    return plugin


@pytest.fixture(autouse=True)
def _reset_stage_cache():
    """The per-run caches are module-global; clear them between tests."""
    _PLUGIN_MOD._STAGED.clear()
    _PLUGIN_MOD._ENDPOINTS.clear()


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


class KollaContainerActionTestCase:
    """The ``kolla_container`` wrapper action plugin."""

    def test_disabled_by_default_is_passthrough(self):
        sink = {}
        _plugin(_TARGET_ARGS, sink).run(task_vars={})
        # No opt-in and nothing OTEL in the spec -> delegated byte-for-byte.
        assert sink["args"] == _TARGET_ARGS
        assert "kolla_otel.managed_env" not in sink["args"]["labels"]

    def test_disabled_strips_instrumentation_from_target(self):
        """With auto_instrument off, a target container's desired spec is
        stripped of any OTEL env/mount/label (by name, like rollback), so a
        deploy/reconfigure recreates it without instrumentation."""
        sink = {}
        args = {
            "action": "recreate_or_restart_container",
            "name": "nova_api",
            "environment": {
                "KOLLA_X": "1",
                "OTEL_SERVICE_NAME": "nova-api",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317",
                "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=openstack",
                "PYTHONPATH": "/otel-auto-instrumentation-python/x",
            },
            "volumes": [
                "/etc/kolla/nova:/var/lib/kolla/config_files:ro",
                "/etc/kolla/opentelemetry/python:"
                "/otel-auto-instrumentation-python:ro",
            ],
            "labels": {
                "kolla_version": "22",
                "kolla_otel.managed_env": (
                    "OTEL_SERVICE_NAME,OTEL_EXPORTER_OTLP_ENDPOINT,"
                    "OTEL_RESOURCE_ATTRIBUTES,PYTHONPATH"
                ),
            },
        }
        _plugin(args, sink).run(task_vars={"otel_auto_instrument": False})
        out = sink["args"]
        # Every managed key gone; kolla's own env preserved.
        assert out["environment"] == {"KOLLA_X": "1"}
        # Agent mount dropped; the config mount stays.
        assert out["volumes"] == [
            "/etc/kolla/nova:/var/lib/kolla/config_files:ro"
        ]
        # Managed label removed; kolla's own labels stay.
        assert "kolla_otel.managed_env" not in out["labels"]
        assert out["labels"]["kolla_version"] == "22"

    def test_disabled_does_not_stage_agent(self):
        """De-instrumentation needs no agent, so the off path makes no host
        module calls (no pull/copy) — only the kolla_container delegate."""
        calls = []
        sink = {}
        plugin = _plugin(_TARGET_ARGS, sink)
        inner = plugin._execute_module

        def _record(module_name, module_args, task_vars):
            calls.append(module_name)
            return inner(module_name, module_args, task_vars)

        plugin._execute_module = _record
        plugin.run(task_vars={"otel_auto_instrument": False})
        assert calls == ["kolla_container"]

    def test_enabled_target_container_is_instrumented(self):
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
        assert (
            "/etc/kolla/nova:/var/lib/kolla/config_files:ro" in args["volumes"]
        )
        # managed-env label records the injected keys
        assert "kolla_otel.managed_env" in args["labels"]
        assert args["labels"]["kolla_version"] == "22"

    def test_endpoint_missing_falls_back_to_local_collector(self):
        """With no external endpoint, instrumentation targets the local
        collector (deployed per host by the otel_collector role)."""
        sink = {}
        _plugin(_TARGET_ARGS, sink).run(
            task_vars={"otel_auto_instrument": True}
        )
        env = sink["args"]["environment"]
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4317"
        assert "kolla_otel.managed_env" in sink["args"]["labels"]

    def test_external_endpoint_is_used_when_set(self):
        sink = {}
        _plugin(_TARGET_ARGS, sink).run(task_vars=dict(_ENABLED))
        env = sink["args"]["environment"]
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4317"

    def test_local_collector_uses_http_port_for_http_protocol(self):
        """Like the role, the local-collector port follows the protocol: the
        collector's HTTP receiver (4318) for http/protobuf, not gRPC (4317)."""
        sink = {}
        _plugin(_TARGET_ARGS, sink).run(
            task_vars={
                "otel_auto_instrument": True,
                "otel_exporter_protocol": "http/protobuf",
            }
        )
        env = sink["args"]["environment"]
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"

    def test_local_collector_uses_api_interface_address(self):
        """With network facts present, the endpoint targets the routable
        api_interface address (not loopback), mirroring the role default."""
        sink = {}
        _plugin(_TARGET_ARGS, sink).run(
            task_vars={
                "otel_auto_instrument": True,
                "api_interface": "eth0",
                "ansible_facts": {"eth0": {"ipv4": {"address": "10.0.0.5"}}},
            }
        )
        env = sink["args"]["environment"]
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://10.0.0.5:4317"

    def test_local_collector_gathers_network_facts_on_demand(self):
        """When facts are absent (gather_facts:false), the plugin gathers the
        network subset on demand to resolve the api_interface address."""
        sink = {}
        plugin = _plugin(_TARGET_ARGS, sink)
        inner = plugin._execute_module
        gathered = []

        def _with_setup(module_name, module_args, task_vars):
            if module_name == "setup":
                gathered.append(module_args.get("gather_subset"))
                return {
                    "ansible_facts": {
                        "eth0": {"ipv4": {"address": "10.9.9.9"}}
                    }
                }
            return inner(module_name, module_args, task_vars)

        plugin._execute_module = _with_setup
        plugin.run(
            task_vars={"otel_auto_instrument": True, "api_interface": "eth0"}
        )
        assert gathered == [["!all", "!min", "network"]]
        env = sink["args"]["environment"]
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://10.9.9.9:4317"

    def test_local_collector_endpoint_override_is_honored(self):
        """An explicit otel_local_collector_endpoint (globals.yml) wins
        verbatim, exactly as the role's set_fact does."""
        sink = {}
        _plugin(_TARGET_ARGS, sink).run(
            task_vars={
                "otel_auto_instrument": True,
                "otel_local_collector_endpoint": "http://collector.local:4317",
            }
        )
        env = sink["args"]["environment"]
        assert (
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.local:4317"
        )

    def test_enabled_non_target_container_is_passthrough(self):
        sink = {}
        args = dict(_TARGET_ARGS, name="rabbitmq")
        _plugin(args, sink).run(task_vars=dict(_ENABLED))
        assert "kolla_otel.managed_env" not in sink["args"]["labels"]

    def test_non_augmentable_action_is_passthrough(self):
        sink = {}
        args = dict(_TARGET_ARGS, action="remove_container")
        _plugin(args, sink).run(task_vars=dict(_ENABLED))
        assert "kolla_otel.managed_env" not in sink["args"]["labels"]

    def test_rollback_action_is_passthrough(self):
        """During otel-rollback the role recreates each container WITHOUT the
        instrumentation; the plugin must step aside (not re-add it) so the
        rollback actually removes the OTEL env/mount/label. Otherwise, with
        otel_auto_instrument enabled, rollback would be silently defeated."""
        sink = {}
        task_vars = dict(_ENABLED, otel_action="rollback")
        # The role's de-instrumented spec: OTEL env already stripped.
        args = dict(_TARGET_ARGS, environment={"KOLLA_X": "1"})
        _plugin(args, sink).run(task_vars=task_vars)
        # Delegated unchanged: no OTEL env re-added, no mount, no label.
        assert sink["args"]["environment"] == {"KOLLA_X": "1"}
        assert "OTEL_SERVICE_NAME" not in sink["args"]["environment"]
        assert "kolla_otel.managed_env" not in sink["args"]["labels"]
        assert not any(
            "otel-auto-instrumentation" in v
            for v in sink["args"].get("volumes", [])
        )

    def test_compare_container_is_made_otel_aware(self):
        """compare_container gets the same overlay so kolla detects the diff
        and fires its recreate handler (which is then augmented too)."""
        sink = {}
        args = {
            "action": "compare_container",
            "name": "nova_api",
            "environment": {"KOLLA_X": "1"},
        }
        _plugin(args, sink).run(task_vars=dict(_ENABLED))
        augmented = sink["args"]
        # The compared desired spec now includes the OTEL env/mount/label, so
        # a not-yet-instrumented running container compares as different.
        assert augmented["environment"]["OTEL_SERVICE_NAME"] == "nova-api"
        assert "PYTHONPATH" in augmented["environment"]
        assert any(
            "/otel-auto-instrumentation-python:ro" in v
            for v in augmented["volumes"]
        )
        assert "kolla_otel.managed_env" in augmented["labels"]

    def test_custom_service_list_is_honored(self):
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

    def test_staging_stages_agent_then_instruments(self):
        """A target is staged (pull + copy) before the overlay is applied."""
        calls = []
        sink = {}
        plugin = _plugin(_TARGET_ARGS, sink)
        inner = plugin._execute_module

        def _record(module_name, module_args, task_vars):
            calls.append((module_name, module_args.get("argv")))
            return inner(module_name, module_args, task_vars)

        plugin._execute_module = _record
        plugin.run(task_vars=dict(_ENABLED))

        # The agent image was pulled and copied out before delegation.
        commands = [argv for n, argv in calls if n == "command"]
        assert any(c[:2] == ["docker", "pull"] for c in commands)
        assert any("cp" in c for c in commands)
        # ...and the container was then instrumented.
        assert "kolla_otel.managed_env" in sink["args"]["labels"]

    def test_staging_failure_is_passthrough(self):
        """If the agent cannot be staged, the container is left uninstrumented
        (mounting an empty dir would break the service)."""
        sink = {}
        _plugin(_TARGET_ARGS, sink, stage_ok=False).run(
            task_vars=dict(_ENABLED)
        )
        assert "kolla_otel.managed_env" not in sink["args"]["labels"]
        assert "OTEL_SERVICE_NAME" not in (
            sink["args"].get("environment") or {}
        )

    def test_check_mode_is_passthrough(self):
        """No staging (and so no instrumentation) during a dry run."""
        sink = {}
        plugin = _plugin(_TARGET_ARGS, sink)
        plugin._task.check_mode = True
        plugin.run(task_vars=dict(_ENABLED))
        assert "kolla_otel.managed_env" not in sink["args"]["labels"]

    def test_fails_open_on_error(self):
        """Any error while instrumenting delegates unmodified, not raise."""
        sink = {}
        plugin = _plugin(_TARGET_ARGS, sink)

        def _boom(module_args, task_vars):
            raise RuntimeError("kaboom")

        plugin._maybe_instrument = _boom
        # Should not raise, and should delegate the original args.
        result = plugin.run(task_vars=dict(_ENABLED))
        assert result == {"changed": False}
        assert sink["args"] == _TARGET_ARGS
