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

## What it does (local mode)

1. Renders `otel_collector_config` to
   `{{ otel_collector_config_dir }}/config.yaml` (default
   `/etc/kolla/otel-collector/config.yaml`).
2. Pulls `{{ otel_collector_image }}`.
3. Creates/recreates the `otel_collector` container (via `kolla_container`,
   `restart_policy: unless-stopped`) with that config bind-mounted read-only at
   the image's default config path. A config-only change restarts the
   container via a handler (kolla's recreate compares the container spec, not
   the mounted file).

## Configuration

The default pipeline is **OTLP in → `debug` out** (received telemetry is
logged), so the collector runs out of the box. Two ways to supply a real
config (the collector config is holistic, so both **replace** the default
rather than merging):

### 1. A config file (recommended) — kolla `node_custom_config` convention

Drop a complete collector config on the **deploy host**, exactly like kolla's
per-service overrides (`/etc/kolla/config/nova/nova.conf`). The role picks the
first that exists, most-specific first:

```
<node_custom_config>/otel-collector/<inventory_hostname>/config.yaml   # per host
<node_custom_config>/otel-collector/config.yaml                        # all hosts
```

`node_custom_config` defaults to `/etc/kolla/config`, so the common case is:

```
/etc/kolla/config/otel-collector/config.yaml
```

Its contents are copied verbatim to the collector container. Point the base
directory elsewhere with `otel_collector_custom_config_dir`.

### 2. The inline `otel_collector_config` variable (globals.yml)

Used only when no override file is found:

```yaml
otel_collector_config:
  receivers:
    otlp:
      protocols:
        grpc: {endpoint: "0.0.0.0:4317"}
        http: {endpoint: "0.0.0.0:4318"}
  processors:
    batch: {}
  exporters:
    otlp/backend:
      endpoint: "tempo.observability.svc:4317"
      tls: {insecure: true}
  service:
    pipelines:
      traces:  {receivers: [otlp], processors: [batch], exporters: [otlp/backend]}
      metrics: {receivers: [otlp], processors: [batch], exporters: [otlp/backend]}
      logs:    {receivers: [otlp], processors: [batch], exporters: [otlp/backend]}
```

## Key variables

See [`defaults/main.yml`](defaults/main.yml). The essentials:

| Variable | Purpose |
| --- | --- |
| `otel_exporter_endpoint` | External collector; **empty** enables this role. |
| `otel_collector_image_registry` / `_repository` / `_tag` | Collector image source (default `docker.io/otel/opentelemetry-collector-contrib:latest`). |
| `otel_collector_custom_config_dir` | Deploy-host dir scanned for an override `config.yaml` (default `<node_custom_config>/otel-collector`). |
| `otel_collector_config` | Inline fallback pipeline config, used when no override file exists. |
| `otel_collector_config_dir` | Host dir holding `config.yaml` (default `/etc/kolla/otel-collector`). |
| `otel_collector_grpc_port` / `_http_port` | OTLP listener ports (4317 / 4318). |
| `otel_collector_restart_policy` | Container restart policy (default `unless-stopped`). |

Keep `otel_collector_grpc_port` in sync with the port in
`otel_local_collector_endpoint` (in the `otel_instrument` role) if you change
it.
