# Self-Healing (core app components across every deployment mode)

The self-heal watchdog is the "self-heal" pillar of the local DevOps/SRE
capstone (#3160): it watches the core app components -- `api`, `web`,
`ollama`, `cassandra` -- however they're deployed (native/local-first,
[Docker Compose](docker-compose.md), [Terraform](terraform.md), or
[Kubernetes](kubernetes.md)) -- plus any running Compose observability
containers, and automatically restarts anything unhealthy or stopped, so a
killed or crashed component recovers without an operator running `docker
restart`/`brew services restart`/`kubectl delete pod` by hand.

It's implemented in `src/nyxgpt/self_heal.py` and runs as a background
thread inside the `api` process -- the same process that already hosts the
[canary](api.md#canary-deployment) deployment logic. In native/local-first
mode (the default local deployment, `nyxgpt ops install`) that's the
Homebrew-managed `nyxgpt-api` service; in a Compose or Terraform deployment
it's the `api`/`nyxgpt-tf-api` container; in a Kubernetes deployment it's
whichever `nyxgpt-api` Pod happens to be running it.

## How it works

Four deployment modes are covered, and a given component is only ever
monitored/healed by one of them at a time (see [Native/local-first
mode](#nativelocal-first-mode) below for how that's decided):

1. **Docker Compose**: every `check_interval_seconds` (default 15s), the
   watchdog runs `docker compose -f <compose file> ps -a --format json` to
   list every container the Compose project has created — the core services
   (`ollama`, `cassandra`, `api`, `web`), if deployed via Compose, plus any
   of the opt-in `monitoring`/`logging`/`tracing`/`errors` profiles that
   happen to be up. A profile/service whose config.ini flag is off (never
   enabled) simply doesn't appear; it isn't treated as "down". A profile
   whose flag IS on but whose containers don't exist at all — e.g. after
   `nyxgpt ops down` — is reported as **absent** and healed the same as an
   unhealthy container; see [Desired state for observability
   profiles](#desired-state-for-observability-profiles) below. See [Docker
   access from inside the `api`
   container](#docker-access-from-inside-the-api-container) for how it
   reaches the Docker daemon and resolves that compose file at all — it
   runs inside one of the containers it's inspecting.
   - A component is **healthy** when its Compose `State` is `running` and
     its `Health` is either `healthy` or empty (no healthcheck configured —
     see [Container healthchecks](#container-healthchecks) below for which
     services have one). Anything unhealthy runs `docker compose restart
     <service>`.
2. **Native/local-first**: `api`/`web`/`ollama` are checked via `brew
   services list` (**healthy** when their state is `started`) and healed
   via `brew services restart <name>`; `cassandra` (the one Docker-managed
   piece of a native install) is checked via `docker ps` (**healthy** when
   `running`) and healed via `docker restart nyxgpt-cassandra` — the same
   mechanisms `nyxgpt ops restart` uses, so the user never needs a raw
   `brew`/`docker` command. See [Native/local-first
   mode](#nativelocal-first-mode) below for details.
3. **Terraform** (`nyxgpt ops install --terraform --local`): `ollama`/
   `cassandra`/`api`/`web` run as the plain (non-Compose) `nyxgpt-tf-*`
   Docker containers `terraform/main.tf` defines. Checked directly via
   `docker ps`/`docker inspect` (**healthy** when `running` and, for the
   three with a Docker `HEALTHCHECK` — `ollama`, `cassandra`, `api` — also
   `healthy`/empty/`starting`) and healed via `docker restart
   nyxgpt-tf-<component>`, the same primitive as native/local-first mode's
   Cassandra container. See [Terraform mode](#terraform-mode) below.
4. **Kubernetes** (`nyxgpt ops install --kubernetes --local`): every
   `nyxgpt-api` Pod (stable/canary — see `k8s/`) is checked via
   `kubectl get pods -n nyxgpt` (**healthy** when `phase=Running` and its
   `Ready` condition is `True`) and healed via `kubectl delete pod`, which
   the owning Deployment's ReplicaSet then recreates. This is on top of, not
   instead of, kubelet's own liveness-probe restarts and `canary.py`'s
   metrics-gated rollout + auto-rollback — see [Kubernetes
   mode](#kubernetes-mode) below.

Regardless of mode:

- Every heal action records an event (service, reason, restart count,
  success/failure) to `~/.nyxGPT/self_heal_state.json`.
- **Backoff and giving up**: a component won't be restarted again within
  `backoff_seconds` (default 30s) of its last restart attempt, and after
  `max_consecutive_restarts` (default 5) consecutive attempts the watchdog
  stops touching it automatically — a component that keeps failing needs a
  human to look at it, not an infinite restart loop. The counter resets to
  0 the next time the component is observed healthy.
- One-shot services (`glitchtip-migrate`, which runs a DB migration and is
  expected to exit 0 and stay exited) are excluded from both the present-container
  check and the desired-service resolution below, so an exited-0 one-shot job is
  never reported "absent"/unhealthy (#3381). If it exits non-zero — a genuinely
  failed migration — it's still surfaced as unhealthy rather than masked.

## Desired state for observability profiles

`docker compose ps -a` only reports containers that exist. That's fine for
detecting a crashed or stopped container, but it can't tell "never started"
apart from "existed, then was torn down entirely" — and `nyxgpt ops down`
does the latter (removes the containers, doesn't touch config.ini). Before
this, tearing a profile's containers down was indistinguishable from never
having enabled it at all: self-heal saw an empty world and had nothing to
heal, even with auto-heal on and the feature flag still enabled (#3356).

Self-heal now also checks config.ini directly for each observability
profile's `enabled` flag:

| config.ini section | Compose profile |
|---|---|
| `[monitoring] enabled` | `monitoring` |
| `[log_aggregation] enabled` | `logging` |
| `[tracing] enabled` | `tracing` |
| `[error_tracking] enabled` | `errors` |

If a section is enabled, its Compose services (resolved via `docker compose
--profile <name> config --services`, the core `nyxgpt`/`api`/`web`/`ollama`/
`cassandra` services excluded — see [Known limitation: the core
stack](#known-limitation-the-core-stack) below — and one-shot services like
`glitchtip-migrate` excluded too, since they're never "desired but absent")
are **desired**. Any desired service missing from `docker compose ps -a`'s
output is reported with
`state: "absent"` (`healthy: false`) instead of not appearing at all, and is
healed via `docker compose --profile ... up -d <service>` rather than
`restart` — there's no container to restart. This is the same set of checks
"Heal all unhealthy now" already runs, so it covers absent components with
no separate code path, and the `/admin/self-heal` dashboard shows an
**Absent** badge (distinct from **Unhealthy**) with the reason ("enabled in
config, no container running").

**Turning a profile off on purpose**: disabling its feature flag in
config.ini (via the [config wizard](configuration.md), which stops but
doesn't remove that profile's containers) is the supported way to keep it
down with auto-heal enabled — self-heal only reconciles against *enabled*
flags. A plain `nyxgpt ops down` with the flag left on and auto-heal on
means the profile comes back on the next heal pass; that's expected, not a
bug.

Because disabling a flag stops rather than removes containers, they still
show up in `docker compose ps -a` as present-but-stopped -- without a
separate check, the automatic heal pass would see that and restart them
right back, undoing the disable. So each present Compose component also
carries a `desired` flag (`true` unless it belongs to a currently-disabled
observability profile): the automatic pass skips restarting a
`desired: false` component entirely (a manual "Heal now" click can still
force it, the same override backoff/max-restarts already get), it's
excluded from the "N unhealthy" count, and the dashboard shows a
**Disabled** badge with the reason ("profile disabled in config, not
auto-healed") instead of a plain **Unhealthy**.

This `desired: false` reconciliation also applies to a one-shot service that
genuinely failed (non-zero exit, so it's still present): disabling its
profile still flags it `desired: false` rather than leaving it stuck
`desired: true` and repeatedly restarted, because the internal check for
"does this present-but-undesired service belong to *some* observability
profile" resolves the full one-shot-inclusive service set for that lookup
specifically, even though one-shot services are excluded everywhere else
(#3381).

### Known limitation: the core stack

This desired-state check (config.ini feature flags) only covers the four
opt-in observability profiles. Whether `ollama`/`cassandra`/`api`/`web`
*should* be running is instead an operator decision, not a config.ini flag
— see [Intentional stops](#intentional-stops-nyxgpt-ops-downstop-vs-self-heal)
below for how that's tracked. A core component stopped by something other
than `nyxgpt ops down`/`stop` (a raw `docker compose down`/`docker stop`/
`brew services stop` run directly — which the wrapped-command policy in
[configuration.md](configuration.md) says not to do anyway) isn't
reconciled by either mechanism, since nothing told self-heal it was
intentional.

## Intentional stops: `nyxgpt ops down`/`stop` vs. self-heal

`nyxgpt ops down`/`stop` (and the SRE dashboard's equivalents) are how an
operator deliberately takes a core component down. Before #3406, self-heal
had no way to tell that apart from a crash: a plain `ops down` stopped
`nyxgpt-api`/`nyxgpt-web`/`ollama` (`brew services stop`) and
`nyxgpt-cassandra` (`docker stop`), all of which stay *installed*, just
stopped — exactly what a crash looks like from the outside. The very next
heal pass would see them unhealthy and restart them right back
(`brew services restart`/`docker restart`), undoing the teardown; worse,
the re-occupied ports then made a subsequent `nyxgpt ops install --terraform
--local` fail with a spurious port collision.

The fix is an **intentional-stop registry**, separate from the `enabled`
flag, persisted in the same `~/.nyxGPT/self_heal_state.json`:

- `nyxgpt ops down`/`stop` mark whichever of `api`/`web`/`ollama`/`cassandra`
  they actually stop (regardless of whether that component was running
  natively or under Compose) as intentionally stopped.
- `list_component_status()` flags a marked component `desired: false` — the
  exact same mechanism (and dashboard **Disabled** badge) already used for a
  disabled observability profile (see above) — so the automatic heal pass
  leaves it alone. A manual "Heal now" click still overrides it, same as
  everywhere else `desired: false` appears.
- `nyxgpt ops install`/`restart` (native, `--terraform --local`, or
  `--kubernetes --local`) clear the marker for whatever they bring up:
  bringing a component back up is itself the "this is desired again" signal.

Critically, this is **per-component**, not a global kill switch: an intentional
`ops down --app-only` (which only stops `api`/`web`/`ollama`/`cassandra`)
leaves the watchdog fully armed to keep healing, say, a crashed `grafana`
container the whole time. The **only** way to arm/disarm the watchdog itself
(the `enabled` flag) is still the `/admin/self-heal` dashboard toggle (or the
equivalent CLI/API) — no `nyxgpt ops` command touches it, before or after
#3406. (Before #3406, `ops down` briefly *did* flip `enabled` off as a
stopgap for the same port-collision bug; that's what this registry replaces.)

## Native/local-first mode

In the default local-first deployment, `nyxgpt-api`/`nyxgpt-web`/`ollama`
run as Homebrew services and `nyxgpt-cassandra` runs as a plain
(non-Compose) Docker container -- none of that is visible to `docker
compose ps`, which previously meant self-heal reported zero core
components outside a Compose deployment (#3348). `src/nyxgpt/self_heal.py`
now checks these directly, in addition to whatever `docker compose ps`
reports:

- `api` → `brew services restart nyxgpt-api`
- `web` → `brew services restart nyxgpt-web`
- `ollama` → `brew services restart ollama`
- `cassandra` → `docker restart nyxgpt-cassandra`

A component is only reported once it's actually installed/created (a brew
service never set up via `nyxgpt ops install`, or a not-yet-created
Cassandra container, is out of scope rather than "down").

**Mode awareness**: if a component is already reported by `docker compose
ps` (i.e. it's deployed via Compose), it is *not* also checked/healed
natively — Compose is presumed to be that component's active deployment.
This mirrors `nyxgpt ops`'s own native/Compose conflict detection (`nyxgpt
ops status`) and means self-heal never starts a competing native service or
restarts a container that isn't actually serving traffic.

Each component's status carries a `source` field (`"native"`, `"compose"`,
`"terraform"`, or `"kubernetes"`) through `GET /api/v1/self-heal/status` and
the `/admin/self-heal` dashboard, so it's clear which mechanism is
monitoring/healing it.

## Terraform mode

`nyxgpt ops install --terraform --local` (see [terraform.md](terraform.md))
runs `ollama`/`cassandra`/`api`/`web` as the plain (non-Compose)
`nyxgpt-tf-ollama`/`nyxgpt-tf-cassandra`/`nyxgpt-tf-api`/`nyxgpt-tf-web`
Docker containers `terraform/main.tf` defines. Like native/local-first
mode's Cassandra container, none of that is visible to `docker compose ps`,
so `src/nyxgpt/self_heal.py` checks these directly via `docker ps`/`docker
inspect` and heals via `docker restart nyxgpt-tf-<component>`:

- `ollama`/`cassandra`/`api` each have a Docker `HEALTHCHECK` (see
  `terraform/main.tf`) — **healthy** when `running` and `Health` is
  `healthy`/empty/`starting` (mirroring the Compose case's health handling).
- `web` has no healthcheck — **healthy** when simply `running`.

**Docker access**: the watchdog runs inside the `nyxgpt-tf-api` container --
the same image (and baked-in Docker CLI) as the Compose `api` service, built
from the repo's root `Dockerfile` — so it needs the same `/var/run/docker.sock`
bind mount to reach the daemon and its sibling `nyxgpt-tf-*` containers;
`terraform/main.tf`'s `docker_container.api` resource mounts it. See [Docker
access from inside the `api`
container](#docker-access-from-inside-the-api-container) for the security
tradeoffs, which apply identically here.

**Mode awareness**: same rule as native mode -- a component already reported
by Compose or native/local-first is not also checked here, so the three
modes never double-heal (or collide on) the same component.

## Kubernetes mode

`nyxgpt ops install --kubernetes --local` (see [kubernetes.md](kubernetes.md))
deploys `nyxgpt-api` as the stable/canary Deployment pair (see `k8s/`);
there's no Kubernetes manifest for `web`/`ollama`/`cassandra`, so this mode
only covers `api`. `src/nyxgpt/self_heal.py` lists every Pod matching
`app=nyxgpt-api-canary-pool` in the `nyxgpt` namespace via `kubectl get
pods` (the watchdog runs inside one of those Pods itself, using the same
`nyxgpt-api` ServiceAccount `canary.py` already uses -- see
`k8s/rbac.yaml`, which also grants `get`/`list`/`delete` on `pods`) and
reports **one `ComponentStatus` per Pod** (not per Deployment: `stable`
alone can run several replicas, each needing its own backoff/restart-count
bookkeeping) -- **healthy** when `phase: Running` and its `Ready` condition
is `True`.

Healing deletes the Pod (`kubectl delete pod`); its Deployment's
ReplicaSet then recreates it. This is **on top of, not instead of**:

- **kubelet's own liveness-probe restarts** (every `deployment-*.yaml`
  already has one -- see [Container healthchecks](#container-healthchecks)),
  which handle an in-place crash without self-heal's help at all.
- **`canary.py`'s metrics-gated rollout and auto-rollback** (see
  [api.md#canary-deployment](api.md#canary-deployment)), which handle a
  systematically-broken *version* by cutting traffic away from it, not an
  individual Pod misbehaving.

`kubectl delete pod` is useful for the gap between those two: a Pod that's
"stuck" (e.g. passing its liveness probe but otherwise wedged) rather than
cleanly crash-looping, where nothing else would touch it.

**Detected mode on the dashboard** (#3410): `self_heal.detected_mode()`
reports which of native/compose/terraform/kubernetes the core components
are currently reporting from, and `/admin/self-heal` shows it plus, in
kubernetes mode specifically, an explicit one-line statement of self-heal's
role above (Pod-level watch on top of kubelet/canary) — so kubernetes mode
reads as "designed and covered" rather than looking indistinguishable from
"no components found."

**A caveat on restart-count bookkeeping**: since a healed Pod is deleted and
recreated under a *new* name, its `restart_counts`/`last_restart_ts` entry in
`~/.nyxGPT/self_heal_state.json` becomes orphaned (the new Pod starts a
fresh entry under its own name) rather than being reused — harmless (state
just grows slightly over time, the same way the bounded `events` log already
does), but worth knowing if you're reading that file directly rather than
through the dashboard/API.

## Turning it on

The watchdog thread always runs once the API starts, but it only takes
action when **enabled** — controlled at runtime, not by editing
`config.ini` and restarting:

- **Dashboard**: `/admin/self-heal` has an "Enable auto-heal" toggle, a
  "Heal now" button per component (and one for "heal everything
  unhealthy"), and a recent-events log.
- **CLI**: `nyxgpt self-heal status` / `enable` / `disable` / `heal
  [--service NAME]`.
- **API**: `GET /api/v1/self-heal/status`, `POST
  /api/v1/self-heal/toggle`, `POST /api/v1/self-heal/heal` — see
  [api.md](api.md#self-heal-watchdog).

`[self_heal] enabled` in `config.ini` (default `false`) only seeds the
*initial* state on a fresh install (`~/.nyxGPT/self_heal_state.json` doesn't
exist yet); once that file exists, the dashboard/CLI/API toggle is the
source of truth and config.ini is no longer consulted.

## Observability: logs, metrics, and the Self-Healing dashboard

Every self-heal decision is logged from `src/nyxgpt/self_heal.py` with
structured fields (via the logging module's `extra={}`, rendered as JSON
when `[logging] format = json` -- see
[configuration.md](configuration.md#logging-section)):

- **Per-component health check** (`self-heal: health check <service>
  healthy=... state=... health=...`) -- logged at `DEBUG` on every check
  (every `check_interval_seconds`) for every component. Set `[logging]
  level = DEBUG` in config.ini to see these; they're intentionally not at
  `INFO` since a healthy stack would otherwise log one line per component
  every 15 seconds forever.
- **Restart attempt** (`self-heal: attempting restart of <service>
  (reason=..., attempt=N)`) and **outcome** (`self-heal: restart of
  <service> succeeded/failed (restart_count=N): <message>`, `INFO` on
  success, `ERROR` on failure) -- logged at `INFO`/`ERROR` since these are
  actual actions, not routine polling.
- **Backoff skip** (`self-heal: skipping restart of <service>, backoff
  active (Xs remaining)`) -- `DEBUG`, since it repeats every check while a
  component is in backoff.
- **Restart-count reset** (`self-heal: <service> recovered, resetting
  consecutive-restart count`) and **giving up** after
  `max_consecutive_restarts` (`self-heal: giving up on <service>, N
  consecutive restart(s) already failed (max=N)`) -- both `INFO`/`WARNING`.
- **Watchdog start/stop** and a **heal-pass summary**
  (`self-heal: heal pass complete (checked=N, unhealthy=N, healed=N,
  manual=bool)`) after every automatic or manual pass -- `INFO`.

**Metrics** (Prometheus, scraped from [`/metrics`](api.md#get-metrics)):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `nyxgpt_selfheal_unhealthy_components` | Gauge | — | Components currently unhealthy or stopped |
| `nyxgpt_selfheal_restarts_total` | Counter | `service`, `result` | Restart attempts, by service and outcome |
| `nyxgpt_selfheal_restart_count` | Gauge | `service` | Current consecutive-restart count per service |
| `nyxgpt_selfheal_last_recovery_timestamp` | Gauge | `service` | Unix timestamp of the last successful restart |
| `nyxgpt_ops_actions_total` | Counter | `command`, `service`, `result` | Operator `nyxgpt ops`/dashboard lifecycle actions -- see [below](#self-heal-restarts-vs-operator-nyxgpt-ops-actions) |

**Grafana dashboard**: `docker/grafana/dashboards/self-healing.json` is
auto-provisioned exactly like the other three dashboards (System Overview,
RAG Performance, API Metrics -- see [docker-compose.md's Monitoring
Dashboards](docker-compose.md#monitoring-dashboards)), no separate install
step. It shows live unhealthy-component count, restarts in the last 24h,
consecutive-restart count per service (the "backoff state" view), restart
rate by service/outcome, time since each service's last recovery, and a
Loki-backed restart/recovery event timeline.

**Loki query** for self-heal events (heal attempts/outcomes) plus operator
`nyxgpt ops` lifecycle events (see below), used by that timeline panel:

```logql
{job="nyxgpt"} |~ `self-heal: .*(restart|heal pass|giving up|recovered)|ops: lifecycle action`
```

Requires the `logging` Compose profile (see [Log
Aggregation](docker-compose.md#log-aggregation)).

### Self-heal restarts vs. operator (`nyxgpt ops`) actions

`nyxgpt_selfheal_restarts_total` counts only the watchdog's own autonomous
restarts -- it answers "how often did the system recover itself." Every
other way a component's lifecycle changes -- `nyxgpt ops install`, `nyxgpt
ops down`, `nyxgpt ops restart`/`stop`, `nyxgpt ops observability`, the
`nyxgpt ops install|down --terraform|--kubernetes --local` paths (CLI-only
since #3410 -- the Infrastructure admin page is status-only and has no
install/destroy controls), and the self-heal page's manual "Heal Now"
button -- is recorded as a separate **ops lifecycle action** instead, via
`src/nyxgpt/ops.py`'s `_record_ops_action`:

- **Metric**: `nyxgpt_ops_actions_total{command, service, result}` (Counter).
  `command` is one of `install`/`down`/`restart`/`stop`/`observability`;
  `service` is the target component (`api`/`web`/`ollama`/`cassandra`/`all`/
  `terraform`/`kubernetes`/`observability`/...); `result` is
  `success`/`failure`/`refused` (e.g. the port-collision refusal from #3193).
- **Log event**: `ops: lifecycle action command=... service=... result=...`,
  structured the same way as self-heal's own log lines (`extra={"component":
  "ops", "event": "ops_lifecycle_action", ...}`), consumed by the same
  restart/recovery timeline panel and Loki query above.

A manual "Heal Now" click on the self-heal page still increments
`nyxgpt_selfheal_restarts_total` too (self-heal's own restart accounting is
unaffected either way), but it *also* records a `nyxgpt_ops_actions_total`
point -- from an incident-review standpoint it was an operator action, not
the watchdog deciding on its own to restart something.

Keeping these separate means a gap in `nyxgpt_selfheal_restarts_total`/the
unhealthy-components gauge can be read correctly: check
`nyxgpt_ops_actions_total` for a `command="down"` or `command="stop"` around
the same time before assuming an unexplained outage.

The SRE/admin dashboard's `/admin/self-heal` page links directly to both
the Grafana Self-Healing dashboard and a Grafana Explore deep link with
this query already loaded (when the `monitoring`/`logging` profiles are
active) -- one click shows matching results, no copy/paste into Explore
required -- so an operator can go from "what's unhealthy right now"
straight to "why" without leaving the app.

That query is scoped to self-heal's own decision log, not the underlying
component's own output -- for a service's raw logs (e.g. Ollama's model
serving output, not just self-heal's restart decisions about it), see
[Ollama logs](api.md#ollama-logs) and [Cassandra logs via Docker
(LaunchAgent)](api.md#cassandra-logs-via-docker-launchagent). Both are
captured into `~/.nyxGPT/logs` automatically by `nyxgpt ops install` and
reach Loki through the same pipeline.

## Docker access from inside the `api` container

The watchdog shells out to `docker compose ps`/`restart`, but it runs
*inside* the `api` container -- a plain `python:3.11-slim` image with no
`docker` CLI, no `/var/run/docker.sock`, and no copy of `docker-compose.yml`
on its filesystem. Left as-is, every one of those calls fails silently
(`list_component_status()` treats "no docker" the same as "nothing to
report"), which is why `/api/v1/self-heal/status` used to permanently show
`"components": []` in the Compose deployment. Three things had to be added
to fix that:

1. **Docker CLI + Compose plugin in the image** (`Dockerfile`): installed
   from Docker's static binaries (`download.docker.com`/GitHub releases)
   rather than the full `docker-ce` apt repo, since only the client is
   needed here, not a daemon.
2. **The Docker socket, bind-mounted into the container**
   (`docker-compose.yml`, `api` service: `/var/run/docker.sock:/var/run/docker.sock`).
   This is what lets the `docker` CLI inside `api` talk to the *host's*
   Docker daemon and see/restart sibling containers.
3. **The compose file itself, bind-mounted in** (`./docker-compose.yml:/etc/nyxgpt/docker-compose.yml:ro`),
   with its in-container path passed to the app via `NYXGPT_COMPOSE_FILE`.
   `self_heal.py` can't locate a compose file relative to its own module
   path the way `nyxgpt` does for other repo-relative lookups (see
   `REPO_ROOT` in `src/nyxgpt/self_heal.py` and `src/nyxgpt/ops.py`) because
   inside the container it isn't part of a checkout at all -- it's
   installed under `site-packages`. The compose file also now pins a
   top-level `name: nyxgpt`, so `docker compose -f <that file> ...` always
   resolves to the running project regardless of what directory a given
   host checked the repo out into (Compose otherwise derives the project
   name from the checkout directory's basename, which the watchdog has no
   way to know from inside a container).

The Terraform deployment's `nyxgpt-tf-api` container is built from the same
`Dockerfile` (so it already has the Docker CLI) and needs only #2, the
socket mount (`terraform/main.tf`'s `docker_container.api` resource) -- it
has no compose file to resolve since Terraform doesn't use Compose at all;
see [Terraform mode](#terraform-mode) above. The security tradeoffs below
apply identically to that container.

### Security tradeoffs of mounting the Docker socket

Mounting `/var/run/docker.sock` into `api` is **effectively root on the
host**: anything with a live connection to that socket can create a new
container with an arbitrary bind mount (e.g. the host's `/`) and a shell,
which is a trivial container-escape-to-root primitive. This is not
mitigated by adding `:ro` to the bind mount -- that only stops the
container from unlinking/replacing the socket *file*, it does not restrict
which Docker API calls can be made once connected to it, which is a common
misconception. Two things follow from that:

- **The blast radius of any RCE in `api` just got materially bigger.** The
  `api` container is the one thing in this stack that parses untrusted
  input end-to-end (chat prompts, uploaded documents for RAG, etc.). Before
  this change, a hypothetical container-escape bug in `api` was contained
  to that container; after this change, it's a host-root escape.
- **This was chosen anyway, deliberately, over the alternative** (moving
  the watchdog to a separate sidecar/host process that owns the socket and
  exposes a narrow internal API for "list status" / "restart X", so `api`
  itself never touches the socket) because the sidecar approach is a
  meaningfully bigger change -- a new always-on service, an internal
  auth boundary between it and `api`, and its own deployment/health story --
  for a capstone-scale local deployment where the threat model is "a
  single operator's own machine or lab environment", not a multi-tenant
  production system. The direct-mount approach is also literally one of
  the two options this issue's acceptance criteria named as acceptable.

If this stack is ever deployed somewhere the "untrusted input reaches a
container with host-root-equivalent access" risk actually matters (e.g. a
shared or internet-facing environment), the recommended hardening path is
one of:

- Put a [Docker socket
  proxy](https://github.com/Tecnativa/docker-socket-proxy) between `api`
  and the real socket, allow-listing only the `containers` resource
  (`ps`/`restart`) and denying everything else (images, volumes, exec,
  swarm, etc.) -- this still permits creating a privileged container via
  `POST /containers/create`, so it narrows but does not close the escape
  primitive above.
- Or move the watchdog out of `api` entirely into a dedicated sidecar (or a
  host-level process/cron job, outside Docker altogether) that is the only
  thing with socket access, communicating with `api` over an internal,
  authenticated channel instead of sharing a process. This closes the gap
  completely at the cost of the added complexity above, and would be the
  right call before running this in a multi-tenant or internet-facing
  environment.

Neither hardening path is implemented here; this section exists so the
tradeoff is a documented, deliberate choice rather than an oversight.

## Container healthchecks

Self-heal can only tell "unhealthy" from "healthy" for containers that have
a Docker `HEALTHCHECK`. These were verified directly against the actual
images (checking for a shell and `wget`/`curl`/`redis-cli`/`python3`, then
running each container and confirming the probe succeeds) rather than
assumed:

| Service | Healthcheck | Probe |
|---|---|---|
| `ollama`, `cassandra`, `api` | pre-existing | (unchanged by this work) |
| `web` | added | `wget --spider http://127.0.0.1:3000/` |
| `prometheus` | added | `wget --spider http://127.0.0.1:9090/-/healthy` |
| `grafana` | added | `wget --spider http://127.0.0.1:3000/api/health` |
| `jaeger` | added | `wget --spider http://127.0.0.1:16686/` |
| `glitchtip-redis` | added | `redis-cli ping` |
| `glitchtip` | added | Python TCP connect to `127.0.0.1:8080` |
| `glitchtip-postgres` | pre-existing | `pg_isready` |
| `loki`, `promtail`, `otel-collector`, `glitchtip-worker` | **none** | see below |

`loki`, `otel-collector`, and `glitchtip` ship images built without a
shell (`opentelemetry-collector-contrib` is FROM-scratch; `loki`'s image
has no `sh`), and `promtail`/`glitchtip-worker` have a shell but no
`wget`/`curl`/`python` to probe an HTTP endpoint with — there is no
`CMD-SHELL` healthcheck possible for them. Self-heal still detects these
four going to a non-`running` state (a crash) via Compose's `State` field;
it just can't distinguish "running but stuck" for them the way it can for
the services above.

## Known limitation: healing the `api` process itself

The same limitation applies in native/local-first mode: the watchdog runs
*inside* the native `nyxgpt-api` Homebrew service's process, so if that
process is killed outright, the watchdog thread dies with it and nothing
restarts it from within the app itself (Homebrew's own `brew services`
supervision may or may not recover it, independent of self-heal). The rest
of this section describes the Compose case specifically.

The watchdog runs *inside* the `api` container. If `api` is killed
(`docker kill` / `docker compose kill api` — not a graceful `docker compose
stop`), the watchdog thread dies with it, so nothing inside the stack
restarts `api` automatically. Docker's own `restart: unless-stopped` policy
does **not** cover this case either: Docker deliberately treats an explicit
`kill`/`stop` as intentional and won't auto-restart the container for it
(confirmed empirically — `restart: unless-stopped` recovers a crash/OOM
fine, but not an explicit kill).

In practice this means: self-heal fully covers `ollama`, `cassandra`,
`web`, and every opt-in-profile container being killed or crashing, but a
killed `api` container currently needs `docker compose up -d api` run by
an operator. `api` now having its own socket access (see [Docker access
from inside the `api` container](#docker-access-from-inside-the-api-container)
above) doesn't change this -- a killed `api` takes the watchdog thread down
with it, socket or no socket. Closing this gap needs a supervisor that
lives *outside* `api` (a separate sidecar or host process), which is out of
scope for this pass — see the open follow-up items in the #3160 issue. The
[smoke test](#smoke-test) below exercises and reports on this explicitly
rather than silently glossing over it.

## Smoke test

`scripts/smoke-test.sh` is the documented end-to-end smoke test: it brings
the stack up, verifies chat and RAG work through the API, kills each core
component one at a time and watches the dashboard's underlying API confirm
recovery, then tears the stack down.

```bash
./scripts/smoke-test.sh              # full run: deploy, verify, kill/heal every component, teardown
./scripts/smoke-test.sh --skip-deploy  # stack is already up; skip straight to verify/kill/heal
./scripts/smoke-test.sh --keep-up      # leave the stack running after the test for manual poking
```

It only requires `cp .env.example .env` with a real `NYXGPT_AUTH_API_KEY`
beforehand (see [docker-compose.md](docker-compose.md)): the script reads
that key and sends it as the `X-API-Key` header on every API call, pulls the
`default_model` from `docker/config.docker.ini` via `/api/v1/models/pull` if
Ollama doesn't already have it, and passes `ensure_schema: true` on the first
RAG ingest so the Cassandra keyspace/table are bootstrapped on a fresh
deploy. For `ollama`, `cassandra`, and `web`, it asserts the watchdog
restores the component to healthy automatically. For `api`, per the
limitation above, it kills the container, confirms self-heal does *not*
recover it (which is expected, not a bug), and then brings it back with
`docker compose up -d api` itself before continuing — this is the one step
in the whole test that isn't hands-off.
