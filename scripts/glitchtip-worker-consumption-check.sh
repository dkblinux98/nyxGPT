#!/usr/bin/env bash
# Does `glitchtip-worker` report healthy while consuming nothing? (#4208)
#
# This script answers one question by RUNNING it, not by reading a manifest:
# with the broker not yet accepting connections at the moment the worker
# process starts, does the worker recover, and does anything say it has not?
#
# It exists because every surface said yes. CI run 39679850907 failed
# `k8s-local-smoke` step 7e with 400 scheduler enqueues and 0 tasks processed
# while the Pod read `1/1 Running` with 0 restarts; #4182 had already read the
# symptom as queue latency and raised the wait from 120s to 240s, which no
# deadline could fix because nothing was in flight. Inspection cannot see any
# of that -- it is a start-up race between two containers, and it needs a
# second machine's worth of timing to show up at all.
#
# So the fault is INJECTED, and both halves are proved (the #3753 template):
#
#   1. unguarded -- the worker starts against a broker that is not listening.
#      Assert it processes nothing, that the heartbeat probe this repo shipped
#      until #4208 calls that healthy, and that the new probe calls it broken.
#      A run where the worker happened to come up fine proves nothing, so the
#      step FAILS if the wedge does not reproduce.
#   2. guarded -- the same image, started only once the broker answers, which
#      is what the `wait-for-redis` initContainer and Compose's `depends_on`
#      enforce. Assert it processes tasks and that the new probe calls it
#      healthy.
#
# The end-to-end proof that the overlay's gate and probes work in a real
# cluster is `k8s-local-smoke` itself: `glitchtip-worker` is in
# `ops.K8S_OBSERVABILITY_DEPLOYMENTS`, so its rollout -- and therefore its new
# readiness probe -- has to pass before that job's step 7e runs at all.
set -euo pipefail

GLITCHTIP_IMAGE="${GLITCHTIP_IMAGE:-glitchtip/glitchtip:6.2.0}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16.14}"
REDIS_IMAGE="${REDIS_IMAGE:-redis:7.4.9-alpine}"
NET="gt-consumption-check-$$"
PROBE_SRC="$(cd "$(dirname "$0")/.." && pwd)/k8s/observability/glitchtip-worker-ready.py"
PROBE_MOUNT=/etc/nyxgpt/probes/glitchtip-worker-ready.py
# How long to give a worker to start consuming before calling it wedged. The
# healthy reference processes its first task within ~20s of start-up (it has
# to boot Django and take the scheduler lock first); 90s is a wide margin on a
# shared runner and still far inside the 240s this defect burned in CI.
SETTLE_S="${SETTLE_S:-90}"

ok() { printf '\033[0;32m[PASS]\033[0m %s\n' "$1"; }
fail() {
    printf '\033[0;31m[FAIL]\033[0m %s\n' "$1" >&2
    # The worker's own log, before the EXIT trap removes the container. The
    # whole subject of this script is a container whose state tells you
    # nothing, so a failure here that printed only a verdict would leave the
    # next reader exactly where #4182 was: a red gate and no evidence.
    printf '\n--- last 60 lines of gt-worker ---\n' >&2
    docker logs gt-worker 2>&1 | tail -60 >&2 || true
    exit 1
}
step() { printf '\n=== %s ===\n' "$1"; }

cleanup() {
    docker rm -f gt-pg gt-redis gt-worker >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

[ -f "$PROBE_SRC" ] || fail "no probe at $PROBE_SRC"

DB_URL=postgres://glitchtip:glitchtip@gt-pg:5432/glitchtip
WORKER_ENV=(
    -e "DATABASE_URL=$DB_URL"
    -e SECRET_KEY=glitchtip-worker-consumption-check
    -e REDIS_URL=redis://gt-redis:6379/0
    -e EMAIL_URL=consolemail://
    -e VTASKS_HEALTH_CHECK_FILE=/tmp/vtasks_health_check
    -e VTASKS_METRICS_PORT=9090
)

# The probe this repo shipped before #4208, verbatim from the docker-compose.yml
# healthcheck it replaced. Kept here so the claim "it was green on the defect"
# is re-measured on every run rather than taken on trust.
HEARTBEAT_PROBE='import os,sys,time
p=os.environ["VTASKS_HEALTH_CHECK_FILE"]
sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<30 else 1)'

start_worker() {
    docker run -d --name gt-worker --network "$NET" "${WORKER_ENV[@]}" \
        -v "$PROBE_SRC:$PROBE_MOUNT:ro" \
        "$GLITCHTIP_IMAGE" ./bin/run-celery-with-beat.sh >/dev/null
}

processed_count() { docker logs gt-worker 2>&1 | grep -c 'Processing task' || true; }
enqueued_count() { docker logs gt-worker 2>&1 | grep -c 'Enqueuing due task' || true; }

new_probe() { docker exec gt-worker python "$PROBE_MOUNT" "$@"; }
old_probe() { docker exec gt-worker python -c "$HEARTBEAT_PROBE"; }

step "fixtures: network, postgres, migrations"
docker network create "$NET" >/dev/null
docker run -d --name gt-pg --network "$NET" \
    -e POSTGRES_USER=glitchtip -e POSTGRES_PASSWORD=glitchtip -e POSTGRES_DB=glitchtip \
    "$POSTGRES_IMAGE" >/dev/null
for _ in $(seq 1 60); do
    docker exec gt-pg pg_isready -U glitchtip >/dev/null 2>&1 && break
    sleep 2
done
docker exec gt-pg pg_isready -U glitchtip >/dev/null 2>&1 || fail "postgres never came up"
docker run --rm --network "$NET" -e "DATABASE_URL=$DB_URL" -e SECRET_KEY=k \
    "$GLITCHTIP_IMAGE" ./manage.py migrate >/dev/null 2>&1 ||
    fail "glitchtip migrations failed"
ok "postgres is accepting and glitchtip is migrated"

# ---------------------------------------------------------------------------
step "1. UNGUARDED: worker starts before the broker is listening"
# ---------------------------------------------------------------------------
# The Service name resolves immediately (the container exists) while the port
# stays closed for 25s -- the shape a Pod sees when it is scheduled alongside
# its broker instead of after it. Reproduced identically with the name not
# resolving at all; this variant is the one a Kubernetes Service produces.
docker run -d --name gt-redis --network "$NET" --entrypoint sh \
    "$REDIS_IMAGE" -c 'sleep 25; exec redis-server' >/dev/null
start_worker
sleep "$SETTLE_S"

unguarded_processed="$(processed_count)"
docker exec gt-redis redis-cli ping >/dev/null 2>&1 ||
    fail "the broker never started listening -- the injection itself is broken"
[ "$unguarded_processed" -eq 0 ] || fail \
    "the wedge did not reproduce ($unguarded_processed task(s) processed), so this run \
proves nothing about the fix -- raise SETTLE_S or re-check the injection"
ok "reproduced: the worker processed 0 tasks ($(enqueued_count) scheduler enqueues) and \
never recovered after the broker came up"

[ "$(docker inspect -f '{{.State.Status}}' gt-worker)" = running ] || fail \
    "the container exited, which Kubernetes/Compose would have restarted -- the defect is \
that it does NOT exit"
[ "$(docker inspect -f '{{.RestartCount}}' gt-worker)" -eq 0 ] ||
    fail "the container restarted on its own; the defect is that nothing makes it"
ok "and it is still 'running' with 0 restarts -- nothing about the container says otherwise"

if old_probe; then
    ok "CONFIRMED BLIND: the pre-#4208 heartbeat probe reports this worker HEALTHY"
else
    fail "the heartbeat probe went red here, so this run is not reproducing the state \
#4208 measured -- re-check the injection before trusting the new probe's verdict"
fi

if new_probe; then
    fail "the #4208 probe reported a worker that has processed nothing as READY"
fi
ok "the #4208 probe reports it NOT ready"

# ---------------------------------------------------------------------------
step "2. GUARDED: the same image, started only once the broker answers"
# ---------------------------------------------------------------------------
# What `wait-for-redis` (Kubernetes) and `depends_on: service_healthy`
# (Compose) enforce, done the way an initContainer does it.
docker rm -f gt-worker >/dev/null
docker run --rm --network "$NET" "$REDIS_IMAGE" sh -c \
    'until redis-cli -h gt-redis -p 6379 ping; do sleep 2; done' >/dev/null ||
    fail "the wait-for-redis gate never passed"
start_worker
sleep "$SETTLE_S"

guarded_processed="$(processed_count)"
[ "$guarded_processed" -gt 0 ] || fail \
    "the gated worker processed nothing either -- the gate is not the fix, or the fixture \
is broken ($(enqueued_count) enqueues)"
ok "the gated worker processed $guarded_processed task(s) ($(enqueued_count) enqueues)"

new_probe || fail "the #4208 probe reported a working worker as not ready"
ok "the #4208 probe reports it ready"

# And the probe's other arm: a stall it has to notice even once the consumer
# HAS run. Seeded with a total nothing will exceed and a last-progress stamp
# well outside the window, which is the later-wedge shape the queue-depth
# check above cannot see.
docker exec gt-worker sh -c \
    'echo "999999 $(python -c "import time;print(time.time()-600)")" > /tmp/stall.state'
if new_probe --state-file /tmp/stall.state; then
    fail "the probe passed a consumer that has made no progress for 600s"
fi
ok "the probe reports a stalled consumer NOT ready"
new_probe --state-file /tmp/stall.state --max-stall-seconds 900 ||
    fail "the probe failed a stall that is still inside its own window"
ok "...and stays ready while the stall is inside the configured window"

printf '\n\033[0;32mAll checks passed.\033[0m %s\n' \
    "The wedge reproduces, the old probe is blind to it, the new probe sees it, and the \
start-up gate prevents it."
