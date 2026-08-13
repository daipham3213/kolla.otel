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
plugin. It makes kolla's desired container spec track the
``otel_auto_instrument`` switch whenever kolla (re)creates a container during
``deploy`` / ``reconfigure``, so the instrumentation state persists across
operations that rebuild services from kolla's own definitions — the Ansible
analogue of the opentelemetry-operator's mutating webhook.

* ``otel_auto_instrument: true`` — **re-apply** OpenTelemetry (env / agent
  bind-mount / managed label) so a targeted container stays instrumented.
* ``otel_auto_instrument: false`` (default) — **strip** any OpenTelemetry from
  the desired spec so a targeted container is recreated without it (a no-op
  when the spec is already clean, which it normally is).

Safety is the overriding concern, because this plugin is in the call path of
*every* ``kolla_container`` task:

* It only ever shapes the create/compare actions (``start_container``,
  ``recreate_or_restart_container`` and ``compare_container``) and only for
  containers in the configured target list. Everything else — a non-target
  container, a non-augmentable action, or an explicit ``otel-rollback`` (which
  the ``otel_instrument`` role de-instruments more precisely) — is passed
  through byte-for-byte. Shaping ``compare_container`` is what makes kolla
  notice a mismatch with the desired state on ``deploy``/``reconfigure`` and
  fire its own recreate handler (which we then shape too); once the running
  container matches, no needless recreate happens.
* It **fails open**: any error while computing the overlay is logged as a
  warning and the original task is run unmodified. It is best effort; it must
  never break a deploy.

The env / bind-mount / label logic is computed by the dependency-free
:mod:`kolla_otel.instrumentation`, the shared source of truth with the
``otel_instrument`` role.
"""

import base64
import json

from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

# Per-process cache of ``(host, image)`` already staged successfully this run,
# so the agent is pulled/copied at most once per language per host per play
# instead of on every kolla_container task that touches it.
_STAGED: set = set()

# Per-process cache of the resolved local-collector endpoint per host, so the
# network facts are gathered at most once per host per run rather than on every
# instrumented kolla_container task. Keyed by ``inventory_hostname``.
_ENDPOINTS: dict = {}

# Emitted once, when Ansible loads this plugin (i.e. it is on the
# action-plugin search path — normally because it was installed adjacent to
# kolla's site.yml). Run any kolla-ansible command with -vvv and grep for this
# line to confirm the plugin is actually loaded; its absence means the
# package's shared-data did not land next to the playbooks (e.g. an editable
# install, or a different prefix from kolla-ansible) and instrumentation can
# never be re-applied.
display.vvv("otel: kolla_container action plugin loaded")


class ActionModule(ActionBase):
    """Shape container-creating ``kolla_container`` tasks to the OTEL state."""

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

    def _local_collector_endpoint(self, task_vars):
        """Resolve this host's local-collector endpoint like the role.

        Mirrors the ``otel_local_collector_endpoint`` role default: an operator
        override (e.g. in globals.yml) wins verbatim; otherwise the endpoint is
        ``http://<api_interface address>:<port>`` with the port chosen by
        ``otel_exporter_protocol`` (``http/protobuf`` -> the collector's HTTP
        port, else its gRPC port), gathering network facts on demand when they
        are absent and falling back to loopback when the address cannot be
        resolved. Cached per host for the run.
        """
        from kolla_otel import instrumentation as instr

        # An operator-set endpoint (globals.yml / group_vars) wins verbatim,
        # exactly as the role's set_fact does when the var is overridden.
        override = self._var(task_vars, "otel_local_collector_endpoint", None)
        if override:
            return str(override)

        host = task_vars.get("inventory_hostname", "")
        if host in _ENDPOINTS:
            return _ENDPOINTS[host]

        protocol = str(
            self._var(
                task_vars,
                "otel_exporter_protocol",
                instr.SCALAR_DEFAULTS["otel_exporter_protocol"],
            )
            or instr.SCALAR_DEFAULTS["otel_exporter_protocol"]
        )
        grpc_port = self._var(
            task_vars,
            "otel_collector_grpc_port",
            instr.DEFAULT_COLLECTOR_GRPC_PORT,
        )
        http_port = self._var(
            task_vars,
            "otel_collector_http_port",
            instr.DEFAULT_COLLECTOR_HTTP_PORT,
        )
        endpoint = instr.local_collector_endpoint(
            self._api_interface_address(task_vars),
            protocol,
            grpc_port,
            http_port,
        )
        _ENDPOINTS[host] = endpoint
        return endpoint

    def _api_interface_address(self, task_vars):
        """Return this host's api_interface address, or ``None``.

        Mirrors the role's endpoint fact lookup (and its on-demand
        ``Gather network facts`` step): read the address of ``api_interface``
        (falling back to ``network_interface``) from network facts, gathering
        them when the play ran with ``gather_facts: false`` and they are
        absent. Returns ``None`` when no interface is configured or the address
        cannot be resolved, so the caller falls back to loopback.
        """
        from kolla_otel import instrumentation as instr

        interface = str(
            self._var(task_vars, "api_interface", None)
            or self._var(task_vars, "network_interface", None)
            or ""
        )
        if not interface:
            return None
        family = str(
            self._var(task_vars, "api_address_family", None)
            or self._var(task_vars, "network_address_family", None)
            or "ipv4"
        )

        facts = task_vars.get("ansible_facts") or {}
        address = instr.interface_address(facts, interface, family)
        if address:
            return address

        # Facts absent (e.g. a play with gather_facts:false, as the role's own
        # play runs): gather the network subset on demand, like the role. Never
        # fatal — on failure or in check mode we return None -> loopback.
        if self._task.check_mode:
            return None
        setup = self._module(
            "setup",
            {"gather_subset": ["!all", "!min", "network"]},
            task_vars,
        )
        facts = setup.get("ansible_facts") or {}
        return instr.interface_address(facts, interface, family)

    def _maybe_instrument(self, module_args, task_vars):
        """Return ``module_args`` shaped for the desired instrumentation state.

        For a targeted container on an augmentable action, the plugin makes
        kolla's desired spec match ``otel_auto_instrument``: when truthy it
        overlays the OTEL env / agent mount / managed label (so a
        deploy/reconfigure keeps the container instrumented); when falsy it
        strips any of those from the spec (so a deploy/reconfigure recreates
        the container without instrumentation). Every other task — non-target
        container, non-augmentable action, or an explicit rollback — is passed
        through untouched. Each declining gate logs its reason at -vvv,
        prefixed ``otel:`` and tagged with the container name, so run any
        kolla-ansible command with -vvv and grep for ``otel:`` to see why.
        """
        from ansible.module_utils.parsing.convert_bool import boolean

        action = self._resolve(module_args.get("action"))
        name = self._resolve(module_args.get("name"))
        label = name or "<unnamed>"

        # Gate 1: never touch the spec during an explicit rollback. The
        # otel-rollback playbook runs the otel_instrument role with
        # otel_action=rollback, whose recreate deliberately strips the OTEL
        # env, drops the agent bind-mount and removes the managed label. If we
        # shaped that recreate we would fight the role's own, more precise
        # (label-based) de-instrumentation. Step aside so the role's spec is
        # what kolla applies. (otel_action is absent during a normal
        # deploy/reconfigure, so this only fires under otel-rollback.)
        otel_action = self._var(task_vars, "otel_action", "instrument")
        if str(otel_action) == "rollback":
            display.vvv(
                f"otel: '{label}': otel_action=rollback -> passthrough "
                "(deferring to the role's de-instrumentation)"
            )
            return module_args

        from kolla_otel import instrumentation as instr

        # Gate 2: only container-create/compare actions carry a spec to shape
        # (compare so kolla notices a mismatch with the desired state and fires
        # its recreate handler; see instrumentation.AUGMENT_ACTIONS).
        if action not in instr.AUGMENT_ACTIONS:
            display.vvv(
                f"otel: '{label}': action '{action}' is not augmentable "
                "-> passthrough"
            )
            return module_args

        # Gate 3: the task must name a container.
        if not name:
            display.vvv("otel: task has no container name -> passthrough")
            return module_args

        # Gate 4: this container must be a configured target.
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

        # Gate 5: the target's language must be known.
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
        host_lib_path = str(
            self._var(
                task_vars, "otel_host_lib_path", instr.DEFAULT_HOST_LIB_PATH
            )
        )

        # Gate 6: the desired state switch. When auto-instrument is OFF, make
        # the spec OTEL-free (strip any managed env / agent mount / label) so a
        # deploy/reconfigure recreates the target WITHOUT instrumentation,
        # rather than leaving whatever is running in place. kolla's own desired
        # spec is normally already clean, so this is usually a no-op; it
        # guarantees the emitted spec carries no OTEL and makes the
        # compare_container check removal-aware.
        if not boolean(
            self._var(task_vars, "otel_auto_instrument", False),
            strict=False,
        ):
            return self._deinstrument(
                module_args, instr, lang, service, task_vars, name, language
            )

        # Gate 7: the agent must be on the host before we mount it. Stage it
        # (pull + copy-out) now, so a plain deploy/reconfigure produces a
        # working instrumentation without a prior `otel-instrument` run. If
        # staging cannot be guaranteed (failure, or check mode) do NOT
        # instrument: mounting an empty dir and pointing PYTHONPATH /
        # JAVA_TOOL_OPTIONS into it would break the service. Passing through
        # leaves the container running as kolla intended.
        if not self._stage_agent(task_vars, language, lang, host_lib_path):
            display.vvv(
                f"otel: '{name}': agent not staged on host -> passthrough"
            )
            return module_args

        # Resolve the exporter endpoint: an external one if configured,
        # otherwise the per-host local collector (deployed by the
        # otel_collector role). It is therefore always well-defined. Resolved
        # only now — after the target/language/staging gates — so we never
        # gather network facts for a container we are not going to instrument.
        endpoint = str(
            self._var(task_vars, "otel_exporter_endpoint", "") or ""
        ) or self._local_collector_endpoint(task_vars)

        # Build the managed OTEL_* environment for this service. The endpoint
        # is the resolved one (external or local collector), not the raw
        # (possibly empty) otel_exporter_endpoint var.
        common_env = {
            env_key: str(self._var(task_vars, var, instr.SCALAR_DEFAULTS[var]))
            for env_key, var in instr.COMMON_ENV_MAP.items()
        }
        common_env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
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
        env_label = str(
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
        labels[env_label] = instr.managed_label_value(managed)
        module_args["labels"] = labels

        if action == "compare_container":
            # Not a recreate: we only make kolla's change-detection OTEL-aware
            # so it recreates (via its handler) when OTEL is missing.
            display.vvv(
                f"otel: '{name}' ({language}): compare is OTEL-aware "
                "(kolla will recreate if not yet instrumented)"
            )
        else:
            display.vvv(
                f"otel: instrumented kolla_container '{name}' ({language})"
            )
        return module_args

    def _deinstrument(
        self, module_args, instr, lang, service, task_vars, name, language
    ):
        """Strip any OTEL overlay from the desired spec (auto-instrument off).

        Mirrors the rollback role's name-based removal: drop every env key this
        project could manage (computed from names, no endpoint needed), the
        agent bind at the language ``mount_path`` and the managed-env label. So
        a deploy/reconfigure recreates the target WITHOUT instrumentation.
        kolla's desired spec is normally already OTEL-free, so each strip only
        rewrites the key when it actually changes something — leaving a clean
        spec byte-for-byte untouched (a true passthrough), yet removing OTEL if
        anything upstream put it there and making compare_container detect an
        instrumented running container as needing recreation.
        """
        removable = instr.managed_env_keys(
            instr.COMMON_ENV_MAP.keys(),
            (self._var(task_vars, "otel_extra_environment", {}) or {}).keys(),
            (service.get("environment") or {}).keys(),
            lang["activation"].keys(),
        )

        environment = module_args.get("environment") or {}
        stripped_env = instr.strip_managed_environment(environment, removable)
        if stripped_env != environment:
            module_args["environment"] = stripped_env

        volumes = module_args.get("volumes")
        if volumes:
            stripped_vols = instr.remove_agent_mount(
                volumes, lang["mount_path"]
            )
            if stripped_vols != list(volumes):
                module_args["volumes"] = stripped_vols

        env_label = str(
            self._var(
                task_vars,
                "otel_managed_env_label",
                instr.DEFAULT_MANAGED_ENV_LABEL,
            )
        )
        labels = module_args.get("labels") or {}
        if env_label in labels:
            module_args["labels"] = {
                k: v for k, v in labels.items() if k != env_label
            }

        display.vvv(
            f"otel: '{name}' ({language}): auto_instrument off -> OTEL kept "
            "out of the desired spec (kolla will recreate without it)"
        )
        return module_args

    # -- staging ---------------------------------------------------------

    def _module(self, name, args, task_vars):
        """Run a module on the target host and return its result dict."""
        return self._execute_module(
            module_name=name, module_args=args, task_vars=task_vars
        )

    @staticmethod
    def _failed(result):
        """True if a module result indicates failure (``failed`` or rc!=0)."""
        return bool(result.get("failed")) or result.get("rc", 0) not in (
            0,
            None,
        )

    def _stage_agent(self, task_vars, language, lang, host_lib_path):
        """Ensure the language's agent is staged on the target host.

        Mirrors the role's ``stage.yml`` but driven from the plugin so a plain
        ``deploy``/``reconfigure`` stages the agent before kolla (re)creates
        the container: pull the image, and (re)copy its artifacts into
        ``<host_lib_path>/<language>`` when the pulled image id differs from
        the recorded marker. Idempotent and cached per (host, image) per run.

        Returns True when the agent is present on the host, False otherwise
        (a failure, or check mode) — the caller then declines to instrument.
        """
        # Never make changes during a dry run.
        if self._task.check_mode:
            display.vvv("otel: check mode -> not staging agent")
            return False

        from kolla_otel import instrumentation as instr

        engine = str(
            self._var(task_vars, "kolla_container_engine", "docker")
            or "docker"
        )
        image = instr.agent_image(
            self._var(
                task_vars, "otel_image_registry", instr.DEFAULT_IMAGE_REGISTRY
            ),
            lang["image_component"],
            self._var(
                task_vars, "otel_image_version", instr.DEFAULT_IMAGE_VERSION
            ),
        )
        stage_dir, marker = instr.stage_paths(host_lib_path, language)
        host = task_vars.get("inventory_hostname", "")
        cache_key = (host, image)
        if cache_key in _STAGED:
            return True

        # Make sure the staging directory exists.
        if self._failed(
            self._module(
                "file",
                {"path": stage_dir, "state": "directory", "mode": "0755"},
                task_vars,
            )
        ):
            return False

        # Pull the image (best effort: fall back to a locally present image so
        # a transient registry outage does not tear down instrumentation).
        pull = self._module(
            "command", {"argv": [engine, "pull", image]}, task_vars
        )
        if self._failed(pull):
            display.vvv(f"otel: pull of {image} failed; trying local image")

        # Resolve the (local) image id; if unavailable, keep an existing stage.
        inspect = self._module(
            "command", {"argv": [engine, "inspect", image]}, task_vars
        )
        if self._failed(inspect):
            existing = self._module("stat", {"path": marker}, task_vars)
            if existing.get("stat", {}).get("exists"):
                _STAGED.add(cache_key)
                return True
            display.warning(f"otel: agent image {image} unavailable on {host}")
            return False
        try:
            image_id = json.loads(inspect["stdout"])[0]["Id"]
        except (KeyError, ValueError, IndexError):
            return False

        # (Re)copy the agent only when the staged image id changed.
        slurp = self._module("slurp", {"src": marker}, task_vars)
        current = None
        if not slurp.get("failed") and slurp.get("content"):
            current = base64.b64decode(slurp["content"]).decode().strip()

        if current != image_id:
            # Empty the directory, then copy the agent out of the image.
            self._module(
                "file", {"path": stage_dir, "state": "absent"}, task_vars
            )
            self._module(
                "file",
                {"path": stage_dir, "state": "directory", "mode": "0755"},
                task_vars,
            )
            source = lang["source_path"].rstrip("/")
            copy = self._module(
                "command",
                {
                    "argv": [
                        engine,
                        "run",
                        "--rm",
                        "--entrypoint",
                        "cp",
                        "--volume",
                        f"{stage_dir}:/otel-dst",
                        image,
                        "-a",
                        f"{source}/.",
                        "/otel-dst/",
                    ]
                },
                task_vars,
            )
            if self._failed(copy):
                display.warning(
                    f"otel: failed to copy agent from {image} on {host}"
                )
                return False
            self._module(
                "copy",
                {"dest": marker, "content": image_id + "\n", "mode": "0644"},
                task_vars,
            )
            display.vvv(f"otel: staged {image} -> {stage_dir} on {host}")

        _STAGED.add(cache_key)
        return True
