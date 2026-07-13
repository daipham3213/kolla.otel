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
"""Action plugin wrapping kolla's ``kolla_container`` module.

Installed adjacent to kolla-ansible's ``site.yml`` (via this package's
shared-data), Ansible auto-loads it as *the* action for every
``kolla_container`` task — exactly like kolla's own ``merge_configs`` action
plugin. It lets us re-apply OpenTelemetry instrumentation whenever kolla
(re)creates a container during ``deploy`` / ``reconfigure``, so instrumentation
persists across operations that rebuild services from kolla's own definitions
— the Ansible analogue of the opentelemetry-operator's mutating webhook.

Safety is the overriding concern, because this plugin is in the call path of
*every* ``kolla_container`` task:

* It is **off by default.** Unless ``otel_auto_instrument`` is truthy *and*
  ``otel_exporter_endpoint`` is set, ``run`` does nothing but delegate to the
  real module — a one-lookup passthrough.
* It only ever augments the two container-creating actions
  (``start_container`` / ``recreate_or_restart_container``) and only for
  containers in the configured target list. Everything else is passed through
  byte-for-byte.
* It **fails open**: any error while computing the overlay is logged as a
  warning and the original task is run unmodified. Instrumentation is best
  effort; it must never break a deploy.

The overlay itself (env / bind-mount / label) is computed by the
dependency-free :mod:`kolla_otel.instrumentation`, the shared source of truth
with the ``otel_instrument`` role.
"""

from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

# Emitted once, when Ansible loads this plugin (i.e. it is on the
# action-plugin search path — normally because it was installed adjacent to
# kolla's site.yml). Run any kolla-ansible command with -vvv and grep for this
# line to confirm the plugin is actually loaded; its absence means the
# package's shared-data did not land next to the playbooks (e.g. an editable
# install, or a different prefix from kolla-ansible) and instrumentation can
# never be re-applied.
display.vvv("otel: kolla_container action plugin loaded")


class ActionModule(ActionBase):
    """Augment container-creating ``kolla_container`` tasks with OTEL."""

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        del tmp  # tmp no longer has any effect

        task_vars = task_vars or {}
        module_args = dict(self._task.args or {})

        try:
            module_args = self._maybe_instrument(module_args, task_vars)
        except Exception as exc:  # never let instrumentation break a deploy
            display.warning(
                "otel: skipping instrumentation for kolla_container task "
                f"(passing through unmodified): {exc}"
            )

        result.update(
            self._execute_module(
                module_name="kolla_container",
                module_args=module_args,
                task_vars=task_vars,
            )
        )
        return result

    # -- helpers ---------------------------------------------------------

    def _resolve(self, value):
        """Template ``value`` via the task's templar (containers included)."""
        if value is None:
            return None
        return self._templar.template(value, fail_on_undefined=False)

    def _var(self, task_vars, name, default=None):
        """Return a (templated) variable from ``task_vars`` or ``default``."""
        if name not in task_vars:
            return default
        return self._resolve(task_vars.get(name))

    def _maybe_instrument(self, module_args, task_vars):
        """Return ``module_args`` augmented with OTEL iff every gate passes.

        Every gate that declines logs the reason at -vvv, prefixed ``otel:``
        and tagged with the container name, so a passthrough is diagnosable:
        run any kolla-ansible command with -vvv and grep for ``otel:`` to see
        exactly which gate stopped a given container from being instrumented.
        """
        from ansible.module_utils.parsing.convert_bool import boolean

        action = self._resolve(module_args.get("action"))
        name = self._resolve(module_args.get("name"))
        label = name or "<unnamed>"

        # Gate 1: explicit opt-in. Off by default so the plugin is a trivial
        # passthrough for everyone who has not enabled auto-instrumentation.
        if not boolean(
            self._var(task_vars, "otel_auto_instrument", False),
            strict=False,
        ):
            display.vvv(
                f"otel: '{label}': otel_auto_instrument not enabled "
                "-> passthrough"
            )
            return module_args

        from kolla_otel import instrumentation as instr

        # Gate 2: only the container-creating actions carry a spec to augment.
        if action not in instr.CREATE_ACTIONS:
            display.vvv(
                f"otel: '{label}': action '{action}' is not a create "
                "action -> passthrough"
            )
            return module_args

        # Gate 3: the task must name a container.
        if not name:
            display.vvv("otel: task has no container name -> passthrough")
            return module_args

        # Gate 4: no exporter endpoint -> nothing to instrument.
        endpoint = str(
            self._var(task_vars, "otel_exporter_endpoint", "") or ""
        )
        if not endpoint:
            display.vvv(
                f"otel: '{label}': otel_exporter_endpoint not set "
                "-> passthrough"
            )
            return module_args

        # Gate 5: this container must be a configured target.
        services = self._var(task_vars, "otel_instrument_services", None)
        if services is None:
            services = instr.DEFAULT_SERVICES
        service = instr.find_service(services, name)
        if service is None:
            display.vvv(
                f"otel: '{label}': not in otel_instrument_services "
                "-> passthrough"
            )
            return module_args

        # Gate 6: the target's language must be known.
        language = service.get("language")
        if language not in instr.LANGUAGE_DEFAULTS:
            display.vvv(
                f"otel: '{label}': unknown language '{language}' "
                "-> passthrough"
            )
            return module_args

        lang = instr.resolve_language(
            language, self._var(task_vars, "otel_languages", None)
        )

        # Build the managed OTEL_* environment for this service.
        common_env = {
            env_key: str(self._var(task_vars, var, instr.SCALAR_DEFAULTS[var]))
            for env_key, var in instr.COMMON_ENV_MAP.items()
        }
        attrs = instr.resource_attributes(
            str(self._var(task_vars, "otel_service_namespace", "openstack")),
            str(self._var(task_vars, "otel_deployment_environment", "") or ""),
            self._var(task_vars, "otel_resource_attributes_extra", {}) or {},
            service.get("resource_attributes") or {},
        )
        managed = instr.managed_environment(
            common_env,
            self._var(task_vars, "otel_extra_environment", {}) or {},
            service.get("otel_service_name") or service.get("name", ""),
            instr.resource_attributes_string(attrs),
            service.get("environment") or {},
            lang["activation"],
        )

        host_lib_path = str(
            self._var(
                task_vars, "otel_host_lib_path", instr.DEFAULT_HOST_LIB_PATH
            )
        )
        label = str(
            self._var(
                task_vars,
                "otel_managed_env_label",
                instr.DEFAULT_MANAGED_ENV_LABEL,
            )
        )

        # Overlay onto kolla's own desired spec. kolla rebuilds the full env
        # on every create, so we simply layer the managed env on top (no
        # stale-key pruning needed, unlike the running-container edit path).
        environment = dict(module_args.get("environment") or {})
        environment.update(managed)
        module_args["environment"] = environment

        module_args["volumes"] = instr.apply_agent_mount(
            module_args.get("volumes"),
            lang["mount_path"],
            instr.agent_bind(host_lib_path, language, lang["mount_path"]),
        )

        labels = dict(module_args.get("labels") or {})
        labels[label] = instr.managed_label_value(managed)
        module_args["labels"] = labels

        display.vvv(
            f"otel: instrumented kolla_container '{name}' ({language})"
        )
        return module_args
