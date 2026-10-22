#!/usr/bin/env bash
# Executed evidence for #4205: a build step that cannot reach the package
# registry fails loudly and is reported as a registry failure.
#
# The question this script answers: what ACTUALLY happens to `web/Dockerfile`'s
# `npm ci` when the registry is unreachable? Not what the Dockerfile says, not
# what a unit test asserts about a string -- what `docker build` does.
#
# It matters because the answer before the fix was not the one everybody
# assumed. #4205 was filed as "a transient npm fetch fails the whole web image
# build". Running the injection found the opposite: `npm ci` **exits 0** with
# the registry unreachable, having created 520 empty package directories and no
# `node_modules/.package-lock.json`, and BuildKit exports and caches that layer.
# The loud failure in the issue was one of two modes; the silent one is worse
# and no job in the repository would have noticed it.
#
# Four phases, and the first two are the fault injection CLAUDE.md's executed
# verification gate asks for (#3775): prove it fails without the fix, prove it
# passes with it.
#
#   1. WITHOUT the fix -- a pre-fix copy of the deps stage (bare `RUN npm ci`),
#      built with `--network none`. Must build SUCCESSFULLY and produce a
#      node_modules containing zero files. That is the defect, demonstrated.
#   2. WITH the fix -- the real `web/Dockerfile`, same injected condition. Must
#      FAIL, must print the incomplete-fetch marker, and the captured output
#      must be attributed to the registry by `build_fetch_policy`.
#   3. The policy is in effect inside the build, not inert: with the network
#      up, `npm config get fetch-retries` inside the deps stage must report the
#      policy's value rather than npm's default of 2.
#   4. A genuine build defect is still reported as a build defect: a missing
#      COPY source, with the network up, must NOT be attributed to the
#      registry. An operator told to "retry, the registry was unreachable"
#      about a broken Dockerfile is sent round a loop that cannot terminate.
#
# Run locally with: ./scripts/image-build-fetch-policy-smoke.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PYTHON="${PYTHON:-python3}"
trap 'rm -rf "$WORK"; docker rmi -f nyxgpt-fetch-policy-prefix \
    nyxgpt-fetch-policy-postfix nyxgpt-fetch-policy-retries \
    nyxgpt-fetch-policy-defect >/dev/null 2>&1 || true' EXIT

log()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

# Classify a captured output through the one policy module, so this script and
# `ops install` cannot disagree about one failure.
classify() {
    PYTHONPATH="$REPO_ROOT/src" "$PYTHON" - "$1" <<'PY'
import sys
from nyxgpt import build_fetch_policy
failure = build_fetch_policy.classify_failure(open(sys.argv[1], encoding="utf-8", errors="replace").read())
print(failure.describe() if failure else "")
PY
}

policy_value() {
    PYTHONPATH="$REPO_ROOT/src" "$PYTHON" -c \
        "from nyxgpt import build_fetch_policy as p; print(getattr(p, '$1'))"
}

# The base image has to be local before `--network none` can build anything.
log "Pre-pulling node:20-alpine (the injection kills the network, not the daemon)"
docker pull -q node:20-alpine >/dev/null

# ---------------------------------------------------------------------------
log "Phase 1/4 -- WITHOUT the fix: bare 'RUN npm ci', registry unreachable"
# ---------------------------------------------------------------------------
mkdir -p "$WORK/prefix"
cp "$REPO_ROOT/web/package.json" "$REPO_ROOT/web/package-lock.json" "$WORK/prefix/"
cat > "$WORK/prefix/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
DOCKERFILE

prefix_status=0
docker build --no-cache --network none --target deps \
    -t nyxgpt-fetch-policy-prefix "$WORK/prefix" >"$WORK/prefix.log" 2>&1 || prefix_status=$?
tail -20 "$WORK/prefix.log"

if [[ $prefix_status -ne 0 ]]; then
    # Honest reporting: if npm has since started failing loudly here, say so.
    # The fix is still required and phases 2-4 still assert it -- but the claim
    # "exit 0 over an empty tree" would no longer be reproducible on this
    # runner's npm, and that is a fact about the evidence, not a pass.
    fail "the pre-fix build FAILED (exit $prefix_status) rather than silently succeeding.
npm's exit-0-over-an-empty-tree behaviour did not reproduce on this runner.
Re-establish the failure mode before trusting phase 2's evidence."
fi

log "Pre-fix build exited 0. Inspecting what it actually installed:"
prefix_files="$(docker run --rm --entrypoint sh nyxgpt-fetch-policy-prefix \
    -c 'find /app/node_modules -type f 2>/dev/null | wc -l' | tr -d '[:space:]')"
prefix_dirs="$(docker run --rm --entrypoint sh nyxgpt-fetch-policy-prefix \
    -c 'ls /app/node_modules 2>/dev/null | wc -l' | tr -d '[:space:]')"
printf '    node_modules entries: %s\n    files inside them:    %s\n' \
    "$prefix_dirs" "$prefix_files"
[[ "$prefix_files" == "0" ]] || fail \
    "expected an empty tree from the pre-fix build; found $prefix_files files. \
The injected condition is not reproducing a failed fetch."
log "DEFECT REPRODUCED: a green build over a node_modules with zero files in it."

# ---------------------------------------------------------------------------
log "Phase 2/4 -- WITH the fix: the real web/Dockerfile, same injected condition"
# ---------------------------------------------------------------------------
postfix_status=0
docker build --no-cache --network none --target deps \
    -t nyxgpt-fetch-policy-postfix "$REPO_ROOT/web" >"$WORK/postfix.log" 2>&1 || postfix_status=$?
tail -25 "$WORK/postfix.log"

[[ $postfix_status -ne 0 ]] || fail \
    "the fixed build SUCCEEDED with the registry unreachable -- the completion \
check in web/Dockerfile is not doing anything."
log "Build failed as it must (exit $postfix_status)."

marker="$(policy_value INCOMPLETE_FETCH_MARKER)"
grep -qF "$marker" "$WORK/postfix.log" || fail \
    "the failed build did not print the incomplete-fetch marker ($marker), so \
nothing attributes it."
log "Marker present: the step says why it failed."

attribution="$(classify "$WORK/postfix.log")"
[[ -n "$attribution" ]] || fail \
    "build_fetch_policy.classify_failure found no reachability evidence in the \
failed build's output -- ops install would report this as a nyxGPT defect."
log "Attributed by the one classifier: $attribution"

# ---------------------------------------------------------------------------
log "Phase 3/4 -- the retry policy is in effect inside the build, not inert"
# ---------------------------------------------------------------------------
expected_retries="$(policy_value RETRIES)"
# Build the real deps stage with the network up, then read npm's effective
# config out of the image. `npm config get` resolves NPM_CONFIG_* exactly as
# `npm ci` does, so this is the policy as npm sees it -- not as we wrote it.
docker build --target deps -t nyxgpt-fetch-policy-retries \
    "$REPO_ROOT/web" >"$WORK/retries.log" 2>&1 || {
        tail -30 "$WORK/retries.log"
        fail "the deps stage failed to build with the network UP -- that is a \
real failure, not an injected one."
    }
effective="$(docker run --rm --entrypoint sh nyxgpt-fetch-policy-retries \
    -c 'npm config get fetch-retries' | tr -d '[:space:]')"
printf '    npm config get fetch-retries -> %s (policy says %s, npm default is 2)\n' \
    "$effective" "$expected_retries"
[[ "$effective" == "$expected_retries" ]] || fail \
    "npm inside the build reports fetch-retries=$effective, not the policy's \
$expected_retries. The ENV stanza is not reaching npm."

log "And the fixed build SUCCEEDS with the network up (the completion check \
does not break the healthy path)."

# ---------------------------------------------------------------------------
log "Phase 4/4 -- a genuine build defect is NOT blamed on the registry"
# ---------------------------------------------------------------------------
mkdir -p "$WORK/defect"
cat > "$WORK/defect/Dockerfile" <<'DOCKERFILE'
FROM node:20-alpine
WORKDIR /app
COPY this-file-does-not-exist.json ./
DOCKERFILE
defect_status=0
docker build --no-cache -t nyxgpt-fetch-policy-defect "$WORK/defect" \
    >"$WORK/defect.log" 2>&1 || defect_status=$?
[[ $defect_status -ne 0 ]] || fail "the deliberately broken build succeeded"
tail -5 "$WORK/defect.log"
misattribution="$(classify "$WORK/defect.log")"
[[ -z "$misattribution" ]] || fail \
    "a genuine build defect was attributed to the registry ($misattribution). \
An operator would be told to retry a failure retrying cannot fix."
log "Correctly NOT attributed to the registry."

printf '\nAll four phases passed: the defect reproduces without the fix, the fix
turns it into a loud and correctly-attributed failure, the retry policy is
live inside the build, and a real build defect is still reported as one.\n'
