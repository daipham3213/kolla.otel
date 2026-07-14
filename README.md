# kolla-otel

Zero-code [OpenTelemetry](https://opentelemetry.io/) auto-instrumentation for
[Kolla Ansible](https://github.com/openstack/kolla-ansible).

`kolla-otel` registers `otel-instrument`, `otel-rollback` and `otel-collector`
commands on the `kolla-ansible` CLI. It injects the [`opentelemetry-operator`](https://github.com/open-telemetry/opentelemetry-operator)
auto-instrumentation agents into OpenStack service containers and configures the
`OTEL_*` environment needed to export telemetry — **without changing any service
code or rebuilding any image** — and can cleanly remove it again.

## How it works

The `otel-instrument` command follows the same pattern as kolla-ansible's
built-in commands (`KollaAnsibleMixin` + `run_playbooks`): it runs the
`otel-instrument.yml` playbook (the [`otel_instrument`](ansible/roles/otel_instrument)
role), which for each target container **present on a host**:

1. Resolves the matching auto-instrumentation image
   (e.g. `ghcr.io/.../autoinstrumentation-python:<tag>`).
2. Stages the agent artifacts from that image into a **host directory**
   (`/etc/kolla/opentelemetry/<language>`) — the Docker/Podman equivalent of
   the operator's Kubernetes init-container.
3. Reads the container's current state (`kolla_container_facts`) and
   **recreates it** (`kolla_container`) with that host path **bind-mounted**
   read-only plus the activation environment (`PYTHONPATH`,
   `JAVA_TOOL_OPTIONS`, `NODE_OPTIONS`, CoreCLR hooks) and the standard
   `OTEL_*` export/resource variables.

The injected env is **declarative**: the managed keys are recorded in a
container label, so removing a variable from config removes it from the
container on the next run (rather than lingering). Only containers already
present are touched, so a run is safe across controllers and compute nodes and
is idempotent on re-run.

Target services (nova-api, nova-conductor, nova-compute, cinder-api,
cinder-volume, keystone, glance-api, neutron-server, …) and all settings are
defined in [`ansible/roles/otel_instrument/defaults/main.yml`](ansible/roles/otel_instrument/defaults/main.yml).

Supported languages: **Python, Java, Node.js, .NET**.

## Install

```bash
pip install .          # into the same environment as kolla-ansible
```

The commands are registered via the `kolla_ansible.cli` entry point and are
discovered automatically:

```bash
kolla-ansible otel-instrument --help
kolla-ansible otel-rollback --help
kolla-ansible otel-collector --help
```

### Older kolla-ansible (no plugin support)

Some kolla-ansible releases (e.g. **18.8.0**) do not load external commands
from the `kolla_ansible.cli` namespace, so `kolla-ansible otel-instrument` is
unavailable there. For those, kolla-otel also installs a self-contained
`kolla-otel` console script exposing the same commands (just without the
`otel-` prefix):

```bash
kolla-otel instrument -i /etc/kolla/inventory
kolla-otel rollback   -i /etc/kolla/inventory
kolla-otel collector  -i /etc/kolla/inventory [--remove]
```

It accepts the same Ansible arguments (`-i/--inventory`, `--configdir`, `-e`,
…) and the same `--config`, and runs the same playbooks — only the command
dispatch is independent of the kolla-ansible CLI version. Use whichever is
available; they are equivalent.

## Usage

Configure the OTLP endpoint (and any overrides) in `globals.yml`:

```yaml
otel_exporter_endpoint: http://otel-collector:4317
otel_deployment_environment: production
```

then run, like any other kolla command:

```bash
kolla-ansible otel-instrument -i /etc/kolla/inventory
```

### Local collector (no external endpoint)

If you **omit** `otel_exporter_endpoint`, `otel-instrument` deploys an
`opentelemetry-collector-contrib` container on **each host** (the
[`otel_collector`](ansible/roles/otel_collector) role) and points
instrumentation at it on the host's own `api_interface` address
(`otel_local_collector_endpoint`, reachable because kolla uses host
networking). The default collector pipeline logs received
telemetry (`debug` exporter); to forward to your real backend, set the
collector's section options (`otel_collector_exporters`,
`otel_collector_service_pipelines`, …) in `globals.yml`/`host_vars` — they are
rendered into a full config by the role's `templates/config.yaml.j2` and
written to every host — or, for file-based deltas, drop a YAML override at
`/etc/kolla/config/otel-collector/config.yaml` (kolla's `node_custom_config`
convention, deep-merged, per-host supported). `otel-rollback` removes the
collector again.

You can also manage that collector **on its own**, without touching any
service, using the `otel-collector` command — handy to stand it up and verify
it before instrumenting, or to run it as a standalone telemetry sink:

```bash
kolla-ansible otel-collector -i /etc/kolla/inventory            # deploy
kolla-ansible otel-collector -i /etc/kolla/inventory --remove   # tear down
```

It runs the `otel_collector` role by itself and, like the automatic path, only
acts when no external `otel_exporter_endpoint` is set (otherwise it is a
no-op). A later `otel-instrument` finds the collector already listening.

Alternatively, pass a standalone config file (validated and translated into
the role's `otel_*` variables):

```bash
kolla-ansible otel-instrument -i /etc/kolla/inventory \
    --config examples/instrumentation.yml
```

See [`examples/instrumentation.yml`](examples/instrumentation.yml) for the
config-file format.

### Rolling back

To remove the instrumentation again — stripping the injected `OTEL_*`
environment, the agent bind-mount and the managed-env label, and recreating
each container back to its pre-instrumentation state — run the mirror command
(idempotent, and safe to point at the same config file):

```bash
kolla-ansible otel-rollback -i /etc/kolla/inventory \
    --config examples/instrumentation.yml
```

### Persisting across deploy/reconfigure

A subsequent `kolla-ansible deploy`/`reconfigure` recreates services from
kolla's own definitions and drops the injected env/volume. To re-apply it
automatically, enable the bundled `kolla_container` action plugin by setting
`otel_auto_instrument: true` (alongside the `otel_*` config) in `globals.yml`.
It is off by default and only ever augments the targeted containers; see the
[role README](ansible/roles/otel_instrument/README.md) for details.

## Ansible content

Playbooks and roles placed under [`ansible/`](ansible/) are shipped into
`share/kolla-ansible/ansible/` — the same prefix Kolla Ansible resolves
playbooks from — with their directory structure preserved. This is declared
in `pyproject.toml` via hatchling's `shared-data`
(`[tool.hatch.build.targets.wheel.shared-data]`); no `setup.py` is required.

## Development

Install the full toolchain and run the checks:

```bash
pip install -e '.[dev]'      # test + lint + type + tox + prek

pytest                        # unit tests (+ coverage)
ruff check kolla_otel         # lint
ruff format kolla_otel        # auto-format
mypy kolla_otel               # type check
```

### Tox

`tox` orchestrates every gate across supported interpreters:

```bash
tox                # py310/py311/py312 tests, then lint + type
tox -e lint        # ruff check + ruff format --check
tox -e type        # mypy
tox -e format      # ruff --fix + ruff format (auto-fix helper)
tox -e precommit   # run all prek/pre-commit hooks
```

### Git hooks (prek / pre-commit)

Hooks are defined in [`prek.toml`](prek.toml) and run with
[`prek`](https://github.com/j178/prek):

```bash
prek install          # install the pre-commit git hook
prek run --all-files   # run every hook manually
```

### Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs three jobs on every
push and pull request: the **test** matrix (Python 3.10–3.12 via tox),
**lint + type** (ruff + mypy), and **pre-commit** (all hooks via prek).

## Package layout

The injection logic lives in Ansible; the Python package is a thin driver.

| Component | Responsibility |
| --- | --- |
| `ansible/roles/otel_instrument` | Stages the agent and recreates each container with the OTel env/volume; also rolls it back (see its [README](ansible/roles/otel_instrument/README.md)) |
| `ansible/roles/otel_collector` | Deploys a per-host local collector when no external endpoint is set (see its [README](ansible/roles/otel_collector/README.md)) |
| `ansible/otel-instrument.yml` / `ansible/otel-rollback.yml` / `ansible/otel-collector.yml` | Playbooks run by the three commands |
| `ansible/action_plugins/kolla_container.py` | Optional wrapper re-applying instrumentation on every kolla container (re)create |
| `kolla_otel.config` | Validated configuration model + loader |
| `kolla_otel.extravars` | Translates the config model into the role's `otel_*` variables |
| `kolla_otel.instrumentation` | Shared, dependency-free overlay logic used by the action plugin |
| `kolla_otel.cli` | The `cliff` commands (`otel-instrument` / `otel-rollback` / `otel-collector`) that run the playbooks |
| `kolla_otel.app` | Self-contained `kolla-otel` console-script app hosting the same commands for kolla-ansible releases without plugin support |

`config` and `extravars` have **no third-party dependencies** and are unit
tested without `cliff` or a live Ansible run.
