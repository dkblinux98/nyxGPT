# Alerting

Real alerting: when a core health signal breaches a threshold, an alert
fires in Grafana's Alerting UI and, if a Slack webhook is configured, a
message posts to Slack. This closes the gap where the System Health
dashboard's "Alerts" panel used to compute its own thresholds independently
of any real monitoring system -- a permanent-looking CRITICAL banner that
corresponded to no actual alert anywhere.

Alerting lives entirely in **Grafana's unified alerting** (not a separate
Alertmanager container, and not Prometheus's own rule evaluation) --
Grafana ships an embedded Alertmanager and queries the same Prometheus
datasource dashboards already use. Everything is provisioned as code under
`docker/grafana/provisioning/alerting/`:

| File | Purpose |
|---|---|
| `rules.yml` | The alert rules themselves (see [Alert rules](#alert-rules) below) |
| `contact-points.yml` | The `nyxgpt-slack` Slack contact point |
| `notification-policies.yml` | Routes every alert to `nyxgpt-slack` by default |

Requires the `monitoring` Compose profile -- see
[docker-compose.md#monitoring-dashboards](docker-compose.md#monitoring-dashboards)
for how to start it (`nyxgpt ops observability` / `nyxgpt ops install`,
never a raw `docker compose` command).

## Alert rules

All rules live in a single Grafana folder, **nyxGPT Alerts**, evaluated
every minute:

| Rule | Signal | Threshold | Severity |
|---|---|---|---|
| NyxGPT API down | `up{job="nyxgpt-api"}` | `< 1` for 1m | critical |
| NyxGPT high error rate | 5xx / total request rate | `> 5%` for 5m | warning |
| NyxGPT high latency | p95 HTTP request duration | `> 2s` for 5m | warning |
| NyxGPT high CPU usage (warning/critical) | `nyxgpt_resource_cpu_percent` | `> 80%` / `> 95%` for 5m | warning / critical |
| NyxGPT high memory usage (warning/critical) | `nyxgpt_resource_memory_percent` | `> 75%` / `> 90%` for 5m | warning / critical |
| NyxGPT high disk usage (warning/critical) | `nyxgpt_resource_disk_percent` (filesystem backing `~/.nyxGPT`) | `> 80%` / `> 90%` for 5m | warning / critical |
| NyxGPT self-heal giving up | `increase(nyxgpt_selfheal_giveup_total[10m])` | `> 0` | critical |
| NyxGPT canary auto-rollback | `increase(nyxgpt_canary_auto_rollback_total[10m])` | `> 0` | warning |

The CPU/memory/disk thresholds mirror the constants in
`src/nyxgpt/health.py` (`MEMORY_WARN_PERCENT`, `CPU_CRITICAL_PERCENT`, etc.)
-- those constants are only the *local fallback* the System Health panel
uses when Grafana is unreachable (see [System Health panel](#system-health-panel)
below), so the two stay in sync deliberately; if you change one, change the
other.

"Self-heal giving up" and "canary auto-rollback" are new Prometheus metrics
added specifically so these events are alertable at all:
`nyxgpt_selfheal_giveup_total{service}` increments each time the self-heal
watchdog exhausts a component's consecutive-restart budget
(see [self-healing.md](self-healing.md)), and
`nyxgpt_canary_auto_rollback_total{component}` increments when `evaluate()`
automatically rolls back a canary rollout due to a metrics regression
(see [kubernetes.md#canary-logging--metrics](kubernetes.md#canary-logging--metrics)).
Both are additive to `/metrics` alongside the existing self-heal/canary
counters -- see [api.md#get-metrics](api.md#get-metrics).

## Slack contact point

The `nyxgpt-slack` contact point reads its webhook URL from
`~/.nyxGPT/secrets/slack-webhook-url` via Grafana's `$__file{}` provisioning
expansion -- the same mechanism `docker/grafana/provisioning/datasources/glitchtip.yml`
already uses for the GlitchTip API token. That file is always kept in sync
with config.ini's `[monitoring] slack_webhook_url` (see
[configuration.md#monitoring-section](configuration.md#monitoring-section))
by `nyxgpt ops env-sync` / `nyxgpt ops install` -- never edit the secrets
file directly.

**Setup:**

1. Create a Slack incoming webhook: <https://api.slack.com/messaging/webhooks>.
2. Set it in config.ini, either directly:
   ```ini
   [monitoring]
   slack_webhook_url = https://hooks.slack.com/services/T00/B00/XXXXXXXX
   ```
   or from the web config wizard (`/admin`) -- it appears as a masked secret
   field under the Observability group in the wizard's Additional Settings
   step, since it's derived automatically from `example.config.ini`.
3. Provision it into Grafana:
   ```bash
   nyxgpt ops env-sync
   ```
   (`nyxgpt ops install` does this automatically on every run.) This
   restarts the `grafana` container if it's already running, since Grafana
   only re-reads `$__file{}` secrets at startup.
4. Verify delivery -- see [Testing the pipeline](#testing-the-pipeline)
   below.

**No webhook configured is a valid, supported state**: alert rules still
evaluate and fire visibly in Grafana's Alerting UI and on the SRE Home
dashboard regardless of whether Slack delivery is configured. When
`slack_webhook_url` is unset, `nyxgpt ops env-sync`/`install` write a
non-functional placeholder URL rather than an empty string (#3538) --
Grafana's alerting-provisioning validator refuses to boot the whole
container on a genuinely empty Slack `url`, so an empty file would crash-loop
Grafana rather than leave it up with a silently-failing contact point. With
the placeholder, Grafana boots normally and only Slack delivery fails
(visible under Grafana's Alerting -> Contact points -> Test) -- alerting
itself is unaffected. If `nyxgpt ops status` ever shows the `grafana` compose
service stuck `restarting`, `nyxgpt ops doctor` reports it and `nyxgpt ops
logs grafana` shows the boot error (most often a bad provisioning file under
`docker/grafana/provisioning/alerting/`).

## Testing the pipeline

A deliberate test that doesn't require actually breaching a threshold:

```bash
nyxgpt ops alert-test
```

This posts a synthetic `NyxGPTAlertTest` alert directly into Grafana's
embedded Alertmanager API -- the same API real firing rules route through
-- so it exercises rules, notification policy, and the Slack contact point
exactly like a genuine alert would. Expect, within a minute or two:

- The alert appears under Grafana's **Alerting -> Fired alerts**.
- If `slack_webhook_url` is configured, a message posts to the configured
  Slack channel.
- It surfaces on the SRE Home dashboard's alerting panel.

The synthetic alert auto-resolves after 5 minutes. See
[ops.md#nyxgpt-ops-alert-test](ops.md#nyxgpt-ops-alert-test) for exit codes
and failure modes.

To test with a *real* threshold breach instead (e.g. to confirm the CPU
rule specifically), load the API process (or lower the rule's threshold
temporarily in `docker/grafana/provisioning/alerting/rules.yml` and restart
Grafana) and watch the same three outcomes above.

You can also use Grafana's own UI: **Alerting -> Contact points ->
nyxgpt-slack -> Test** sends a single test notification through that
contact point without going through a rule or the Alertmanager API at all
-- useful for isolating "is the webhook URL itself valid" from "is the
whole rules-to-notification pipeline wired up".

## System Health panel

The admin System Health dashboard's (`/admin/health`) "Alerts" panel is
**not** a second, independent alert computation. `GET /api/v1/admin/health`
prefers Grafana's real firing-alert state
(`nyxgpt.health.fetch_grafana_alerts`, querying the same Alertmanager API
`alert-test` above uses) and only falls back to a local threshold snapshot
(`nyxgpt.health.compute_alerts`) when monitoring is disabled or Grafana is
unreachable. The response's `alerts_source` field (`"grafana"` or `"local"`)
is shown in the panel so it's always clear which one you're looking at --
this is what makes the previous class of drift (a local-only alert with no
real backing) structurally impossible: when Grafana is reachable, its state
*is* the panel's state.
