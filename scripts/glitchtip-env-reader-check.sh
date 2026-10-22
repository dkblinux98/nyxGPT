#!/usr/bin/env bash
# Does the pinned GlitchTip image read the variables nyxGPT sets on it? (#4212)
#
# This script answers that by RUNNING the image, because the question is about
# the inside of a container and no amount of manifest review can see in. #4212
# is what happens when nobody does: both GlitchTip containers carried
# `CELERY_WORKER_AUTOSCALE: "1,3"` and
# `CELERY_WORKER_MAX_TASKS_PER_CHILD: "10000"` on both substrates, the image
# has no reader for either, and the values were wrong in both directions --
# they named a 1-3 worker pool where `runworker` builds a 20-lane one, and
# `CELERY_WORKER_AUTOSCALE` was taken for a live setting and named as one of
# three candidate causes while diagnosing #4208.
#
# `tests/unit/test_third_party_container_env.py` guards the manifests against
# regrowing them. It is a static check over YAML, so it can only enforce the
# claims in its `ENV_READERS` table; this script is what keeps that table
# honest against the image itself, and it proves four things:
#
#   1. the `CELERY_*` pair has ZERO readers anywhere in the image, and there is
#      no `celery` binary or importable module -- so removing them removed
#      nothing from the running system;
#   2. `VTASKS_CONCURRENCY` IS read from the environment -- the knob that
#      actually exists, named in the comments that replaced the dead pair;
#   3. `VTASKS_QUEUES` is NOT read from the environment, even though it is a
#      real `django-vtasks` setting, because `glitchtip/settings.py` assigns it
#      and the env fallback only fires for settings Django leaves unset. This
#      is the trap: setting it would read as tuning and tune nothing, which is
#      #4212's own defect under a more plausible name;
#   4. the default concurrency fits the container's memory limit with room to
#      spare -- the measurement behind the decision to leave it alone, taken
#      under the real 1GiB limit rather than asserted in a comment.
#
# `bin/run-worker.sh` is what both substrates now invoke, so (5) also proves
# that script exists in the pinned image and that the
# `bin/run-celery-with-beat.sh` it replaced was only a wrapper around it.
set -euo pipefail

GLITCHTIP_IMAGE="${GLITCHTIP_IMAGE:-glitchtip/glitchtip:6.2.0}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16.14}"
REDIS_IMAGE="${REDIS_IMAGE:-redis:7.4.9-alpine}"
NET="gt-env-reader-check-$$"

# The limit the worker runs under in k8s/observability/glitchtip.yaml. The
# memory assertion below is against this number, so if the manifest's limit
# changes this value must change with it -- that is the point of measuring
# against it rather than against a round number.
WORKER_MEMORY_LIMIT="${WORKER_MEMORY_LIMIT:-1g}"
WORKER_LIMIT_MIB=1024
# Headroom demanded of the measurement. The worker measured ~105MiB at the
# image's default concurrency of 20; 50% of the limit is a wide margin that
# still fails loudly if a future image or setting change makes the default
# untenable -- at which point `VTASKS_CONCURRENCY` becomes worth setting and
# this script is where that is discovered.
MAX_RSS_MIB=512
# Long enough for the worker to boot Django, take the scheduler lock and run
# its first tasks: the healthy reference processes its first within ~20s.
SETTLE_S="${SETTLE_S:-75}"

ok() { printf '\033[0;32m[PASS]\033[0m %s\n' "$1"; }
fail() {
    printf '\033[0;31m[FAIL]\033[0m %s\n' "$1" >&2
    # Same reasoning as glitchtip-worker-consumption-check.sh: a red gate that
    # printed only a verdict leaves the next reader where #4208 started.
    if docker inspect gt-env-worker >/dev/null 2>&1; then
        printf '\n--- last 40 lines of gt-env-worker ---\n' >&2
        docker logs gt-env-worker 2>&1 | tail -40 >&2 || true
    fi
    exit 1
}
step() { printf '\n=== %s ===\n' "$1"; }

cleanup() {
    docker rm -f gt-env-pg gt-env-redis gt-env-worker >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
step "1. the CELERY_* pair has no reader in $GLITCHTIP_IMAGE"
# ---------------------------------------------------------------------------
# Searched over the application, the task library and the scripts on PATH --
# every place a reader could be. `grep -rl` over the whole filesystem would
# also match this script's own name in a mounted checkout, so the roots are
# named explicitly.
celery_readers="$(docker run --rm --entrypoint sh "$GLITCHTIP_IMAGE" -c '
set -e
roots="/code /usr/local/bin"
for d in /usr/local/lib/python*/site-packages/django_vtasks; do
    [ -d "$d" ] && roots="$roots $d"
done
grep -rl "CELERY_WORKER_AUTOSCALE\|CELERY_WORKER_MAX_TASKS_PER_CHILD" $roots 2>/dev/null | wc -l
')"
[ "$celery_readers" -eq 0 ] || fail \
    "something in the image matches the CELERY_* names ($celery_readers file(s)) -- \
#4212 removed them on the grounds that nothing does, so re-check before trusting that"
ok "0 files under /code, django_vtasks or /usr/local/bin mention either CELERY_* name"

docker run --rm --entrypoint sh "$GLITCHTIP_IMAGE" -c 'command -v celery' >/dev/null 2>&1 &&
    fail "a celery binary exists in the image; #4212's premise is wrong"
docker run --rm --entrypoint sh "$GLITCHTIP_IMAGE" -c 'python -c "import celery"' >/dev/null 2>&1 &&
    fail "celery is importable in the image; #4212's premise is wrong"
ok "no celery binary on PATH and no importable celery module"

# ---------------------------------------------------------------------------
step "2. bin/run-worker.sh is what the deprecated wrapper only ever exec'd"
# ---------------------------------------------------------------------------
# Both substrates call `run-worker.sh` directly since #4212. Prove the file is
# there and that the name they used before was a wrapper around it, so the
# change is the same process under the name of what runs.
docker run --rm --entrypoint sh "$GLITCHTIP_IMAGE" -c '[ -x bin/run-worker.sh ]' ||
    fail "bin/run-worker.sh is missing or not executable -- both substrates invoke it"
docker run --rm --entrypoint sh "$GLITCHTIP_IMAGE" -c \
    'grep -q "exec bin/run-worker.sh" bin/run-celery-with-beat.sh' ||
    fail "bin/run-celery-with-beat.sh no longer just execs run-worker.sh, so swapping \
the command was not the no-op #4212 claimed"
ok "bin/run-worker.sh exists and bin/run-celery-with-beat.sh only execs it"

# ---------------------------------------------------------------------------
step "3. VTASKS_CONCURRENCY is read from the environment; VTASKS_QUEUES is not"
# ---------------------------------------------------------------------------
# Resolved through django-vtasks' own settings object, with Django configured
# exactly as the image configures it, which is the only thing that can tell
# the two apart: `VtasksSettings.__getattr__` consults the environment ONLY
# when `getattr(django_settings, name)` is None, and glitchtip/settings.py
# assigns VTASKS_QUEUES while leaving VTASKS_CONCURRENCY unset.
RESOLVE='
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
django.setup()
from django_vtasks.conf import settings as vt
print("CONCURRENCY=%r QUEUES=%r" % (vt.VTASKS_CONCURRENCY, vt.VTASKS_QUEUES))
'
resolve() {
    docker run --rm --entrypoint sh \
        -e SECRET_KEY=glitchtip-env-reader-check \
        -e DATABASE_URL=postgres://u:p@127.0.0.1:5432/d \
        "$@" "$GLITCHTIP_IMAGE" -c "python -c '$RESOLVE'" 2>/dev/null |
        grep -o 'CONCURRENCY=.*'
}

baseline="$(resolve)"
[ -n "$baseline" ] || fail "could not resolve django-vtasks settings inside the image"
printf '    unset:                  %s\n' "$baseline"
case "$baseline" in
    *"CONCURRENCY=20"*) ok "the image default concurrency is 20, as the manifests' comments state" ;;
    *) fail "the image's default concurrency is not 20 ($baseline) -- the comments in \
docker-compose.yml and k8s/observability/glitchtip.yaml cite 20 and must be corrected" ;;
esac

concurrency_set="$(resolve -e VTASKS_CONCURRENCY=4)"
printf '    VTASKS_CONCURRENCY=4:   %s\n' "$concurrency_set"
case "$concurrency_set" in
    *"CONCURRENCY=4"*) ok "VTASKS_CONCURRENCY IS read from the environment -- the real knob" ;;
    *) fail "VTASKS_CONCURRENCY was ignored ($concurrency_set). The manifests name it as \
the knob to use instead of the removed CELERY_* pair; if the image stopped reading it, \
that guidance is now wrong in the same way #4212 was" ;;
esac

queues_set="$(resolve -e VTASKS_QUEUES=ingest)"
printf '    VTASKS_QUEUES=ingest:   %s\n' "$queues_set"
case "$queues_set" in
    *"QUEUES=['default', 'ingest']"*)
        ok "VTASKS_QUEUES is IGNORED from the environment (settings.py wins) -- setting it \
would be #4212 again" ;;
    *) fail "VTASKS_QUEUES now responds to the environment ($queues_set). That inverts the \
comments in both manifests, which tell the next reader not to set it because nothing \
would read it -- update them in the same change" ;;
esac

# ---------------------------------------------------------------------------
step "4. the default concurrency fits the worker's memory limit"
# ---------------------------------------------------------------------------
# The justification #4212 AC2 asks for, measured rather than asserted. If the
# default ever stops fitting, this is where it surfaces -- and THAT is when
# setting VTASKS_CONCURRENCY becomes the right change.
docker network create "$NET" >/dev/null
docker run -d --name gt-env-pg --network "$NET" \
    -e POSTGRES_USER=glitchtip -e POSTGRES_PASSWORD=glitchtip -e POSTGRES_DB=glitchtip \
    "$POSTGRES_IMAGE" >/dev/null
docker run -d --name gt-env-redis --network "$NET" "$REDIS_IMAGE" >/dev/null
for _ in $(seq 1 60); do
    docker exec gt-env-pg pg_isready -U glitchtip >/dev/null 2>&1 && break
    sleep 2
done
docker exec gt-env-pg pg_isready -U glitchtip >/dev/null 2>&1 || fail "postgres never came up"
DB_URL=postgres://glitchtip:glitchtip@gt-env-pg:5432/glitchtip
docker run --rm --network "$NET" -e "DATABASE_URL=$DB_URL" -e SECRET_KEY=k \
    "$GLITCHTIP_IMAGE" ./manage.py migrate >/dev/null 2>&1 ||
    fail "glitchtip migrations failed"

# No VTASKS_CONCURRENCY here on purpose: the subject is the DEFAULT, because
# leaving it at the default is the decision being justified.
docker run -d --name gt-env-worker --network "$NET" --memory "$WORKER_MEMORY_LIMIT" \
    -e "DATABASE_URL=$DB_URL" -e SECRET_KEY=k \
    -e REDIS_URL=redis://gt-env-redis:6379/0 -e EMAIL_URL=consolemail:// \
    -e VTASKS_METRICS_PORT=9090 \
    "$GLITCHTIP_IMAGE" ./bin/run-worker.sh >/dev/null
sleep "$SETTLE_S"

[ "$(docker inspect -f '{{.State.Status}}' gt-env-worker)" = running ] ||
    fail "the worker is not running at the image default concurrency under \
$WORKER_MEMORY_LIMIT -- which would itself mean the default does not fit the limit"
rss_bytes="$(docker exec gt-env-worker sh -c \
    'cat /sys/fs/cgroup/memory.current 2>/dev/null || cat /sys/fs/cgroup/memory/memory.usage_in_bytes')"
rss_mib=$((rss_bytes / 1024 / 1024))
threads="$(docker exec gt-env-worker sh -c "awk '/Threads/{print \$2}' /proc/1/status")"
processed="$(docker logs gt-env-worker 2>&1 | grep -c 'Processing task' || true)"
pg_conns="$(docker exec gt-env-pg psql -U glitchtip -d glitchtip -tAc \
    "select count(*) from pg_stat_activity where datname='glitchtip'" | tr -d ' ')"

printf '    %sMiB of a %sMiB limit | %s threads | %s postgres connections | %s tasks processed\n' \
    "$rss_mib" "$WORKER_LIMIT_MIB" "$threads" "$pg_conns" "$processed"
[ "$processed" -gt 0 ] || fail \
    "the worker processed nothing, so this is not a measurement of a working worker \
($(docker logs gt-env-worker 2>&1 | grep -c 'Enqueuing due task' || true) enqueues)"
[ "$rss_mib" -lt "$MAX_RSS_MIB" ] || fail \
    "the worker used ${rss_mib}MiB of its ${WORKER_LIMIT_MIB}MiB limit at the image's \
default concurrency, past the ${MAX_RSS_MIB}MiB this check allows. The #4212 decision to \
leave VTASKS_CONCURRENCY alone rested on this being far below the limit -- revisit it, and \
set VTASKS_CONCURRENCY on both substrates together"
ok "the default concurrency runs in ${rss_mib}MiB of a ${WORKER_LIMIT_MIB}MiB limit while \
processing tasks -- the limit is not a reason to cap it"

printf '\n\033[0;32mAll checks passed.\033[0m %s\n' \
    "The removed CELERY_* pair had no reader, VTASKS_CONCURRENCY does, VTASKS_QUEUES does \
not, and the default concurrency fits the limit with room to spare."
