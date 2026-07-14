# `otel_collector` role

Deploys a local [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
(`opentelemetry-collector-contrib`) container on **each** Kolla Ansible host,
to serve as the OTLP endpoint for the instrumented services — but only when no
external collector is configured.

## When it runs

The role keys off `otel_exporter_endpoint` (from the `otel_instrument` role /
`globals.yml`):

- **empty** → *local collector mode*: deploy `otel_collector` on every host;
  instrumentation targets `otel_local_collector_endpoint`
  (default `http://127.0.0.1:4317`).
- **set** (external collector) → the role is a **no-op**.

kolla runs every container with host networking, so a collector bound to
`0.0.0.0:4317` is reachable from the (also host-networked) service containers
over loopback — no ports or links to wire up.

It is included by the `otel-instrument.yml` playbook (before `otel_instrument`,
so the collector is listening before services point at it) and by
`otel-rollback.yml` (with `otel_action=rollback`, which stops/removes the
collector and deletes its config).

It can also be run **on its own**, independently of instrumentation, via the
`otel-collector.yml` playbook / the `kolla-ansible otel-collector` command — so
operators can deploy and verify the collector first, then instrument later:

```bash
kolla-ansible otel-collector -i /etc/kolla/inventory            # deploy
kolla-ansible otel-collector -i /etc/kolla/inventory --remove   # tear down
```

`--remove` simply passes `otel_action=rollback` to the same role.

## What it does (local mode)

1. Assembles the full collector config from the role's `otel_collector_*`
   section options via [`templates/config.yaml.j2`](templates/config.yaml.j2),
   optionally deep-merges an operator override, and writes the result to
   `{{ otel_collector_config_dir }}/config.yaml` (default
   `/etc/kolla/otel-collector/config.yaml`) **on each host in the inventory**
   (see [Configuration](#configuration)).
2. Pulls `{{ otel_collector_image }}`.
3. Creates/recreates the `otel_collector` container (via `kolla_container`,
   `restart_policy: unless-stopped`) with that config bind-mounted read-only at
   the image's default config path. A config-only change restarts the
   container via a handler (kolla's recreate compares the container spec, not
   the mounted file).

## Configuration

The full config is built by `templates/config.yaml.j2` from one variable per
top-level collector section, then written to every host. The defaults form a
functional **OTLP in → `debug` out** pipeline (received telemetry is logged),
so the collector runs out of the box.

### 1. Section options (recommended)

Set these in `globals.yml`, `group_vars`, or `host_vars` (the last two give
per-host/-group configs natively). Each maps 1:1 to a collector config section;
empty optional sections (`extensions`, `connectors`, `service.extensions`,
`service.telemetry`) are omitted from the rendered file:

| Variable | Collector section |
| --- | --- |
| `otel_collector_extensions` | `extensions` |
| `otel_collector_receivers` | `receivers` |
| `otel_collector_processors` | `processors` |
| `otel_collector_exporters` | `exporters` |
| `otel_collector_connectors` | `connectors` |
| `otel_collector_service_extensions` | `service.extensions` |
| `otel_collector_service_pipelines` | `service.pipelines` |
| `otel_collector_service_telemetry` | `service.telemetry` |

For example, to also export to a real backend, add the exporter and list it in
the pipelines (keeping the default receivers/processors):

```yaml
# globals.yml (or host_vars/<host>.yml for a per-host endpoint)
otel_collector_exporters:
  debug: {verbosity: normal}
  otlp/backend:
    endpoint: "tempo.observability.svc:4317"
    tls: {insecure: true}
otel_collector_service_pipelines:
  traces:  {receivers: [otlp], processors: [resource, batch], exporters: [otlp/backend]}
  metrics: {receivers: [otlp], processors: [resource, batch], exporters: [otlp/backend]}
  logs:    {receivers: [otlp], processors: [resource, batch], exporters: [otlp/backend]}
```

### 2. A config-file override (advanced) — kolla `node_custom_config` convention

For deltas you'd rather keep in a file, drop a YAML config on the **deploy
host**, alongside kolla's per-service overrides. The role picks the first that exists,
most-specific first, and **deep-merges** it onto the assembled config:

```
<node_custom_config>/otel-collector/<inventory_hostname>/config.yaml   # per host
<node_custom_config>/otel-collector/config.yaml                        # all hosts
```

`node_custom_config` defaults to `/etc/kolla/config`, so the common case is
`/etc/kolla/config/otel-collector/config.yaml` (or the per-host path for a
single host). Point the base directory elsewhere with
`otel_collector_custom_config_dir`.

Merge semantics (`combine(recursive=True)`): nested mappings merge and the
override wins; **lists are replaced wholesale**, so redeclare the full list for
any pipeline you touch:

```yaml
# /etc/kolla/config/otel-collector/config.yaml
exporters:
  otlp/backend:
    endpoint: "tempo.observability.svc:4317"
    tls: {insecure: true}
service:
  pipelines:
    traces:  {receivers: [otlp], processors: [resource, batch], exporters: [otlp/backend]}
    metrics: {receivers: [otlp], processors: [resource, batch], exporters: [otlp/backend]}
    logs:    {receivers: [otlp], processors: [resource, batch], exporters: [otlp/backend]}
```

## Key variables

See [`defaults/main.yml`](defaults/main.yml). The essentials:

| Variable | Purpose |
| --- | --- |
| `otel_exporter_endpoint` | External collector; **empty** enables this role. |
| `otel_collector_image_registry` / `_repository` / `_tag` | Collector image source (default `docker.io/otel/opentelemetry-collector-contrib:latest`). |
| `otel_collector_receivers` / `_processors` / `_exporters` / `_extensions` / `_connectors` | The collector config sections rendered by `templates/config.yaml.j2`. |
| `otel_collector_service_pipelines` / `_service_extensions` / `_service_telemetry` | The `service:` block (pipeline wiring, enabled extensions, self-telemetry). |
| `otel_collector_custom_config_dir` | Deploy-host dir scanned for an optional `config.yaml` override, deep-merged onto the rendered config (default `<node_custom_config>/otel-collector`). |
| `otel_collector_config_dir` | Host dir holding `config.yaml` (default `/etc/kolla/otel-collector`). |
| `otel_collector_config_mode` | Mode of the rendered config file (default `0644`). The contrib image runs as a **non-root** user (UID 10001) and reads the file over a read-only bind mount, so it must be world-readable; tighten only if you also chown it to the collector's UID out of band. |
| `otel_collector_grpc_port` / `_http_port` | OTLP listener ports (4317 / 4318). |
| `otel_collector_restart_policy` | Container restart policy (default `unless-stopped`). |

Keep `otel_collector_grpc_port` in sync with the port in
`otel_local_collector_endpoint` (in the `otel_instrument` role) if you change
it.

## Troubleshooting

**Container exits immediately with `config.yaml: permission denied`:**

```
Error: failed to get config: cannot resolve the configuration: ...
  unable to read the file file:/etc/otelcol-contrib/config.yaml:
  open /etc/otelcol-contrib/config.yaml: permission denied
```

The contrib image runs as a non-root user (UID 10001) and cannot read a
root-owned config that is not world-readable. The role writes the file
`0644` (`otel_collector_config_mode`) to avoid this; if you overrode that mode
or manage the file yourself, ensure the collector user can read it (`chmod o+r`
or chown it to UID 10001). Re-running the playbook re-applies the mode and
restarts the collector.
