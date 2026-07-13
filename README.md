# kolla-otel

Zero-code [OpenTelemetry](https://opentelemetry.io/) auto-instrumentation for
[Kolla Ansible](https://github.com/openstack/kolla-ansible).

`kolla-otel` registers `otel-instrument` and `otel-rollback` commands on the
`kolla-ansible` CLI. It injects the [`opentelemetry-operator`](https://github.com/open-telemetry/opentelemetry-operator)
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
```

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
| `ansible/otel-instrument.yml` / `ansible/otel-rollback.yml` | Playbooks run by the two commands |
| `kolla_otel.config` | Validated configuration model + loader |
| `kolla_otel.extravars` | Translates the config model into the role's `otel_*` variables |
| `kolla_otel.cli` | The `cliff` commands (`otel-instrument` / `otel-rollback`) that run the playbooks |

`config` and `extravars` have **no third-party dependencies** and are unit
tested without `cliff` or a live Ansible run.
