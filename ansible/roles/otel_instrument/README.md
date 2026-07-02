# `otel_instrument` role

Injects the [`opentelemetry-operator`](https://github.com/open-telemetry/opentelemetry-operator)
auto-instrumentation agent and the `OTEL_*` environment into running Kolla
Ansible service containers, then recreates them — the Docker/Podman equivalent
of the operator's Kubernetes init-container pattern.

## What it does, per target container present on a host

1. **Stages the agent** from the language's auto-instrumentation image
   (e.g. `autoinstrumentation-python`) into a named volume
   (`otel_auto_instrumentation_<service>_<language>`), once.
2. **Reads the container's current state** with `kolla_container_facts`
   (image, environment, binds, healthcheck, privileged/pid/ipc mode).
3. **Recreates the container** with `kolla_container`
   (`recreate_or_restart_container`), adding:
   - the agent volume mounted read-only at the language's `mount_path`;
   - the `OTEL_*` export/resource variables and the language activation
     variables (`PYTHONPATH`, `JAVA_TOOL_OPTIONS`, `NODE_OPTIONS`, CoreCLR
     hooks) merged on top of the existing environment.

Only containers that already exist on a host are touched, so a single run is
safe across controllers and compute nodes. The step is idempotent: a second
run finds the env and volume already present and makes no change.

## Key variables

See [`defaults/main.yml`](defaults/main.yml). The essentials:

| Variable | Purpose |
| --- | --- |
| `otel_exporter_endpoint` | **Required.** OTLP collector endpoint. |
| `otel_exporter_protocol` | `grpc` (default) or `http/protobuf`. |
| `otel_deployment_environment` | Optional `deployment.environment` attribute. |
| `otel_image_registry` / `otel_image_version` | Agent image source/tag. |
| `otel_instrument_services` | List of `{name, container_name, language}` targets. |
| `otel_languages` | Per-language image, mount path and activation env. |

## Caveats

- **Kolla owns the container spec.** A subsequent `kolla-ansible deploy`/
  `reconfigure` recreates services from kolla's own definitions and will drop
  the injected env/volume. Re-run `kolla-ansible instrument` afterwards (or
  fold the settings into kolla via a pull request) to reapply.
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
