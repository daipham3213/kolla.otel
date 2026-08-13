# `otel_instrument` role

Injects the [`opentelemetry-operator`](https://github.com/open-telemetry/opentelemetry-operator)
auto-instrumentation agent and the `OTEL_*` environment into running Kolla
Ansible service containers, then recreates them — the Docker/Podman equivalent
of the operator's Kubernetes init-container pattern.

## What it does, per target container present on a host

1. **Pulls the agent image and stages it** into a host directory
   (`{{ otel_host_lib_path }}/<language>`, default
   `/etc/kolla/opentelemetry/<language>`), once per language. The pulled
   image id is recorded in `.<language>-image-id`; the agent is (re)copied
   only when that id changes, so a moved tag / newer image is picked up
   automatically on the next run.
2. **Reads the container's current state** with `kolla_container_facts`
   (image, environment, binds, healthcheck, privileged/pid/ipc mode).
3. **Recreates the container** with `kolla_container`
   (`recreate_or_restart_container`), adding:
   - that host directory **bind-mounted** read-only at the language's
     `mount_path`;
   - the `OTEL_*` export/resource variables and the language activation
     variables (`PYTHONPATH`, `JAVA_TOOL_OPTIONS`, `NODE_OPTIONS`, CoreCLR
     hooks).

### Declarative environment

The injected environment is **declarative**, not additive. The role records
the set of env keys it manages in a container label
(`otel_managed_env_label`, default `kolla_otel.managed_env`). On each run it:

1. reads that label to learn which keys it set last time,
2. strips those keys (and the ones it is about to set) from the container's
   current env — leaving the base image / kolla env untouched,
3. applies the currently-desired managed set and rewrites the label.

So **removing a variable from config removes it from the container** on the
next run, instead of the old value lingering forever.

Only containers that already exist on a host are touched, so a single run is
safe across controllers and compute nodes. The step is idempotent: a second
run with unchanged config pulls the image, finds the recorded image id and the
managed env unchanged, and makes no change. Bumping `otel_image_version` (or a
moved tag) re-stages the agent and recreates the affected containers.

## Rolling back

`otel_action=rollback` (run it via `kolla-ansible otel-rollback` or the
`otel-rollback.yml` playbook) undoes instrumentation. For every targeted
container present on a host that this role previously instrumented, it
recreates the container back to its pre-instrumentation state:

1. reads the `otel_managed_env_label` to learn which env keys were injected,
2. strips those keys — plus, as a safety net, every key the role *could* have
   managed (computed from names alone, so no exporter endpoint is needed) —
   leaving the base image / kolla env untouched,
3. drops the agent bind-mount at the language's `mount_path` and removes the
   managed-env label,
4. recreates the container (preserving privileged / pid / ipc mode,
   capabilities and healthcheck exactly like the inject path).

Removing the label and the bind-mount reliably triggers kolla's recreate (its
env comparison is additive-only and would not notice removed keys on its own),
so the reduced environment actually takes effect. Once every targeted
container on a host is rolled back, the staged agent artifacts under
`otel_host_lib_path` are deleted too — set `otel_rollback_remove_agent=false`
to keep them for a quick re-instrument.

Rollback is **idempotent**: a container with no managed label, no agent mount
and no managed env keys is left untouched, so a second run (or rolling back a
never-instrumented container) is a no-op.

## Persisting across `deploy` / `reconfigure` (auto-instrument)

By default a subsequent `kolla-ansible deploy`/`reconfigure` recreates
services from kolla's own definitions and drops the injected env/volume (see
Caveats). To make instrumentation **survive those operations automatically**,
this package ships a `kolla_container` **action plugin**
([`ansible/action_plugins/kolla_container.py`](../../action_plugins/kolla_container.py)),
installed next to kolla's `site.yml`, so Ansible auto-loads it as the action
for every `kolla_container` task — the Ansible analogue of the
opentelemetry-operator's mutating admission webhook. Whenever kolla
(re)creates a targeted container it re-applies the OTEL env, agent bind-mount
and managed-env label.

Because it sits in the path of *every* `kolla_container` task, it is
deliberately conservative:

- **Tracks the switch.** For a targeted container it makes kolla's desired
  spec match `otel_auto_instrument`: when `true` (put it, `otel_exporter_endpoint`
  and the rest of the `otel_*` config in `globals.yml`) it re-applies the OTEL
  env/mount/label so instrumentation survives `deploy`/`reconfigure`; when
  `false` (the default) it strips any OTEL env/mount/label from the spec so the
  container is recreated **without** instrumentation. kolla's own spec is
  normally already clean, so the `false` path is usually a no-op — but it means
  flipping the switch off and running `deploy`/`reconfigure` reliably removes
  instrumentation, rather than leaving whatever is running in place.
- **Narrow scope.** It only shapes the create/compare actions
  (`start_container`, `recreate_or_restart_container`, `compare_container`)
  and only for containers in `otel_instrument_services`. Every other task —
  and an explicit `otel-rollback` (deferred to the role's more precise,
  label-based de-instrumentation) — is passed through untouched. Shaping
  `compare_container` is what makes kolla notice a mismatch with the desired
  state on `deploy`/`reconfigure` and fire its own recreate handler; once the
  running container matches, nothing is recreated needlessly.
- **Fails open.** Any error while computing the overlay is logged as a warning
  and the original task runs unmodified — it is best effort and never breaks a
  deploy.

The overlay logic is shared with this role via the dependency-free
`kolla_otel.instrumentation` module (a test keeps the Python copy of the
defaults in sync with `defaults/main.yml`).

**Release compatibility.** Modern kolla-ansible drives containers through the
`kolla_container` module; 2023.1 and earlier use `kolla_docker`. The plugin
ships under **both** names (`action_plugins/kolla_container.py` and a
`kolla_docker.py` that re-exports the same class) and delegates to whichever
module Ansible invoked it as, so deploy/reconfigure persistence works on either
release. (The `otel-instrument` / `otel-rollback` **playbooks** themselves use
the modern `kolla_container_facts` interface and target current kolla-ansible;
the action plugin is what carries 2023.1 support.)

**Agent staging is automatic.** Before it mounts the agent, the plugin stages
it on the host itself — pulling the image and copying the artifacts into
`otel_host_lib_path` (mirroring the role's `stage.yml`), once per language per
host per run — so a plain `deploy`/`reconfigure` produces working
instrumentation with no prior `otel-instrument` run. If staging cannot be
completed (image pull/copy fails, or during `--check`) the plugin declines to
instrument and passes the task through unchanged, so the service still starts
rather than booting against an empty mount.

## Key variables

See [`defaults/main.yml`](defaults/main.yml). The essentials:

| Variable | Purpose |
| --- | --- |
| `otel_action` | `instrument` (default) or `rollback`. |
| `otel_rollback_remove_agent` | On rollback, also delete staged agent artifacts from the host (default `true`). |
| `otel_auto_instrument` | Desired-state switch for the `kolla_container` action plugin: `true` keeps targets instrumented across `deploy`/`reconfigure`; `false` (default) keeps them de-instrumented (strips OTEL on recreate). |
| `otel_exporter_endpoint` | **Required** (for `instrument`). OTLP collector endpoint. |
| `otel_exporter_protocol` | `grpc` (default) or `http/protobuf`. |
| `otel_deployment_environment` | Optional `deployment.environment` attribute. |
| `otel_image_registry` / `otel_image_version` | Agent image source/tag. |
| `otel_host_lib_path` | Host base dir the agent is staged into (default `/etc/kolla/opentelemetry`). |
| `otel_extra_environment` | Extra env applied to **every** service (map). |
| `otel_managed_env_label` | Container label recording managed env keys (default `kolla_otel.managed_env`). |
| `otel_instrument_services` | List of `{name, container_name, language}` targets; each entry also accepts optional `otel_service_name`, `resource_attributes` and `environment` (per-service extra env). |
| `otel_language_defaults` | Built-in per-language image, mount path and activation env (source of truth). |
| `otel_languages` | Per-language **overrides**, deep-merged onto `otel_language_defaults` (set only the keys you change). |

## Caveats

- **Kolla owns the container spec.** A subsequent `kolla-ansible deploy`/
  `reconfigure` recreates services from kolla's own definitions and will drop
  the injected env/volume. Re-run `kolla-ansible otel-instrument` afterwards, enable
  the auto-instrument action plugin (see *Persisting across deploy/reconfigure*
  above), or fold the settings into kolla via a pull request to reapply.
- Custom `dimensions` (ulimits/memory limits) are **not** reconstructed on
  recreate; healthcheck, privileged, pid/ipc mode and capabilities are.

## Per-service notes

### `nova_compute`

`nova_compute` is the notable special case: kolla runs it **privileged** and
with **`ipc_mode: host`** (it talks to libvirt/QEMU on the host). Because the
role reads the running container's `HostConfig` back and re-applies it, these
are preserved automatically on recreate — the recreate task passes:

- `privileged: {{ HostConfig.Privileged }}`  → `true`
- `ipc_mode: {{ HostConfig.IpcMode }}`      → `host`
- `pid_mode`, `cap_add`, `security_opt`      → whatever the container had
- `healthcheck`                              → rebuilt from `Config.Healthcheck`
  (nanoseconds → seconds, all keys populated) so the compute health probe is
  not lost

So no `nova_compute`-specific override is needed. The only attribute not
carried over is custom `dimensions`; if you set nova-compute ulimits/memory
limits via kolla, re-run `kolla-ansible reconfigure` (which will also drop the
injected env/volume — re-run `instrument` afterwards).

The `ansible` CI job / `tox -e ansible` runs `ansible-lint` (production
profile) over the role, so this recreate logic stays syntactically valid and
idiomatic.
