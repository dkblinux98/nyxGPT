#!/usr/bin/env bash
# Executed evidence for #4133: does a `brew upgrade` on a host with the stack
# running leave the api executing the NEW keg's venv?
#
# The defect this reproduces, from v3.0.0 acceptance on 2026-10-03: a
# `brew upgrade` took `nyxgpt-api@3.0.0rc` from rc14 to rc17, `nyxgpt ops
# install` reported 56/56 steps `[OK]` including a service restart, and
# `nyxgpt ops status` reported the new version -- while the process answering
# on :8000 was a python3.11 venv under `~/.nyxGPT/opt/nyxgpt-api/venv` that
# the upgrade had emptied. It survived only while it held the deleted files
# open; the first restart after that killed the api with
# `ModuleNotFoundError: No module named 'anyio._backends'`.
#
# Why this needs a real machine. Every claim in it is about what a *process*
# is doing: that a keg's service really execs `libexec/venv/bin/python3`, that
# a pre-upgrade process really survives the keg that started it, that
# `sys.prefix` really moves when the service is restarted onto the new keg. A
# unit test can model the path shapes; it cannot produce a surviving process
# whose interpreter has been deleted underneath it. D-006 applies, and no
# existing job covers it -- `macos-brew-smoke.yml`'s other jobs install one
# version and stop.
#
# The staged condition, and why it is staged rather than waited for. The
# process the owner was left with was accounted for by NO service manager:
# `brew services list` did not name it and no LaunchAgent plist described it,
# which is exactly why `ops._retire_previous_identity`'s discovery sweep could
# not see it (that sweep reads brew's rows and the plists on disk). So this
# script produces that state deliberately -- stop the service, relaunch the
# old keg's wrapper detached from launchd, then upgrade and remove the old keg
# -- rather than hoping a hosted runner reproduces an eight-week-old install.
#
# Both halves are measured, in that order, because a job that only runs the
# fixed code is green on every machine that fails to reproduce the bug (the
# rc5 lesson, #3753):
#
#   1. the defect, injected: the installed keg's own `running_build.same_tree`
#      is reverted to an unconditional match -- the pre-#4133 world, where
#      nothing compared the running process to the installed build -- and the
#      install step and `ops status` are measured reporting [OK] over the
#      stale process.
#   2. the fix, restored: the same two surfaces have to name the mismatch,
#      the step has to repair it, and `/api/v1/info` has to come back
#      reporting the NEW keg's venv.
#
# The injection edits the INSTALLED KEG, never the checkout, and asserts the
# edit landed -- a no-op sed would make half 1 pass by looking like half 2.
#
# Usage:
#   macos-upgrade-smoke.sh <old version> <new version> <tap> <new formula .rb>
# e.g.
#   macos-upgrade-smoke.sh 3.0.0rc0 3.0.0rc1 nyxgpt/upgrade-smoke dist/rc1/nyxgpt-api@3.0.0rc.rb
#
# Exits non-zero on the first thing it cannot establish, with the reason.

set -uo pipefail

OLD_VERSION="${1:-}"
NEW_VERSION="${2:-}"
TAP="${3:-}"
NEW_RB="${4:-}"
if [ -z "$OLD_VERSION" ] || [ -z "$NEW_VERSION" ] || [ -z "$TAP" ] || [ -z "$NEW_RB" ]; then
  echo "usage: $0 <old version> <new version> <tap> <new formula .rb>" >&2
  exit 64
fi
# Absolute, because everything below runs from $HOME: a user's install has no
# checkout above it, and a repo-relative read here is #3759's defect class.
NEW_RB="$(cd "$(dirname "$NEW_RB")" && pwd)/$(basename "$NEW_RB")"

RELEASE="${OLD_VERSION%%rc*}"
FORMULA="nyxgpt-api@${RELEASE}rc"
API_URL="http://127.0.0.1:8000"

WORK="$(mktemp -d)"
# Where the superseded keg is kept so the survivor can be staged more than
# once; populated in step 3, declared here because `set -u` is on.
KEG_BACKUP="$WORK/old-keg"
FAILURES=0

log() { printf '\n=== %s ===\n' "$*"; }
pass() { printf '  [OK] %s\n' "$*"; }
fail() {
  printf '  [FAIL] %s\n' "$*"
  echo "::error::$*"
  FAILURES=$((FAILURES + 1))
}
die() {
  fail "$*"
  echo "Nothing below this point would measure anything, so stopping here."
  exit 1
}

# `curl` writes the body to $2 and prints the status code. 000 is the answer
# this script wants for "nothing is listening", so curl's exit code is
# deliberately ignored -- same contract as macos-user-path-smoke.sh.
http_code() {
  local code
  code="$(curl -s -o "$2" -w '%{http_code}' --max-time 15 "$1" 2>/dev/null)"
  echo "${code:-000}"
}

wait_for_http() {
  local url="$1" body="$2" seconds="${3:-180}"
  local deadline=$((SECONDS + seconds)) code="000"
  while [ "$SECONDS" -lt "$deadline" ]; do
    code="$(http_code "$url" "$body")"
    [ "$code" = "200" ] && break
    sleep 3
  done
  echo "$code"
}

wait_for_silence() {
  local url="$1" seconds="${2:-60}"
  local deadline=$((SECONDS + seconds))
  while [ "$SECONDS" -lt "$deadline" ]; do
    [ "$(http_code "$url" /dev/null)" = "000" ] && return 0
    sleep 2
  done
  return 1
}

# The one read this whole script is about: what the process answering on :8000
# says it is executing. Prints "<prefix>\t<python>\t<pid>\t<prefix_exists>".
runtime_report() {
  local body="$WORK/info.json"
  local code
  code="$(http_code "$API_URL/api/v1/info" "$body")"
  if [ "$code" != "200" ]; then
    echo -e "\t\t\t"
    return 1
  fi
  python3 - "$body" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
runtime = data.get("runtime") or {}
print(
    "\t".join(
        [
            str(runtime.get("prefix") or ""),
            str(runtime.get("python") or ""),
            str(runtime.get("pid") or ""),
            "yes" if runtime.get("prefix_exists") else "no",
        ]
    )
)
PY
}

realpath_of() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

# Stage #4133's defining state: a nyxGPT api serving :8000 that NO service
# manager accounts for, executing the superseded keg's venv.
#
# A function rather than a straight line through the script because the two
# repairs under test -- the install step and the remediation every surface
# PRINTS -- each have to be measured from this state, and the first one to run
# consumes it. That is the hole the review of #4155 found: `nyxgpt ops restart
# api` was executed only after the install step had already removed the
# survivor, so the gate could not see that a bare `brew services restart` is a
# no-op against a process launchd never knew about.
#
# `$1` is the keg to launch from; it is restored from `$KEG_BACKUP` if a
# previous stage's cleanup removed it. Restored to the SAME absolute path on
# purpose: a venv's `pyvenv.cfg` and console-script shebangs are absolute.
stage_survivor() {
  local old_keg="$1"
  if [ ! -d "$old_keg" ]; then
    test -d "$KEG_BACKUP" || { echo "::error::no keg backup to restage from"; return 1; }
    cp -R "$KEG_BACKUP" "$old_keg" || return 1
  fi
  # `nohup` + `disown`, not a plain background job: a background job inherits
  # this shell's process group and dies with the step. The staged process must
  # outlive everything that knows about it, which is the whole point.
  brew services stop "$FORMULA" 2>&1 | tail -5
  wait_for_silence "$API_URL/health" 90 || {
    echo "::error::the service did not stop, so the next process would lose the :8000 bind race"
    return 1
  }
  nohup "$old_keg/bin/nyxgpt-api" > "$WORK/stale.log" 2>&1 &
  local shell_pid=$!
  disown "$shell_pid" 2>/dev/null || true
  if [ "$(wait_for_http "$API_URL/health" "$WORK/health.json" 240)" != "200" ]; then
    cat "$WORK/stale.log"
    echo "::error::the detached $old_keg wrapper never answered -- no survivor to stage"
    return 1
  fi
  # The defining property, asserted every time: nothing registered accounts
  # for this process. A staging that left brew reporting the formula started
  # would be measuring an ordinary restart, not #4133.
  brew services list | tee "$WORK/services-staged.log"
  if grep -qE "^${FORMULA}[[:space:]]+started" "$WORK/services-staged.log"; then
    echo "::error::brew still reports $FORMULA as started -- this is not the unaccounted-for process #4133 was about"
    return 1
  fi
  return 0
}

cd "$HOME" || exit 1

# ---------------------------------------------------------------------------
# Precondition. Asserted, never skipped: a script that quietly stops measuring
# services when launchd is unavailable reports green while asserting nothing,
# which is the hollow-gate shape #3860 exists to remove.
# ---------------------------------------------------------------------------
log "precondition: brew services is usable on this machine"
brew services list > "$WORK/services-precheck.log" 2>&1 \
  || { cat "$WORK/services-precheck.log"; die "brew services is not usable on this runner"; }
cat "$WORK/services-precheck.log"
pass "brew services answers"

CELLAR="$(brew --cellar)"
OLD_KEG="$CELLAR/$FORMULA/$OLD_VERSION"
NEW_KEG="$CELLAR/$FORMULA/$NEW_VERSION"

# ---------------------------------------------------------------------------
# 1. The old candidate, installed and serving.
# ---------------------------------------------------------------------------
log "install $FORMULA $OLD_VERSION and prove its service runs the keg's own venv"
test -d "$OLD_KEG" || die "$OLD_KEG is not installed -- the caller was meant to install it first"

# Seeded from the INSTALLED keg's own packaged resources, with the keg's own
# python -- never from a checkout. `nyxgpt ops status` and the running-build
# probe both read the api port out of this file, and the wizard is
# interactive by design (docs/live-verification-ci.md). Same read, same
# reasoning, as macos-user-path-smoke.sh: an artifact install that lost its
# packaged resources fails here rather than three steps later (#3759).
OLD_KEG_PY="$OLD_KEG/libexec/venv/bin/python3"
test -x "$OLD_KEG_PY" || die "no venv python in $OLD_KEG -- the $OLD_VERSION keg is not self-contained"
"$OLD_KEG_PY" - <<'PY' || die "the installed package could not produce its own example.config.ini"
import importlib.resources as resources
import pathlib

dst = pathlib.Path.home() / ".nyxGPT" / "config.ini"
if dst.exists():
    print(f"config already present: {dst}")
else:
    src = resources.files("nyxgpt.resources") / "example.config.ini"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"seeded {dst} from the packaged example.config.ini")
PY
test -f "$HOME/.nyxGPT/config.ini" || die "no ~/.nyxGPT/config.ini -- the probe cannot find the api port"

brew services start "$FORMULA" 2>&1 | tail -5
CODE="$(wait_for_http "$API_URL/health" "$WORK/health.json" 240)"
[ "$CODE" = "200" ] || die "the $OLD_VERSION keg's service never answered $API_URL/health (got $CODE)"
pass "$OLD_VERSION is serving"

IFS=$'\t' read -r OLD_PREFIX OLD_PY OLD_PID OLD_EXISTS < <(runtime_report)
echo "  running prefix: $OLD_PREFIX (python $OLD_PY, pid $OLD_PID, prefix exists: $OLD_EXISTS)"
# Non-vacuity. If the keg's service does not run the keg's own venv, there are
# no two builds here to tell apart and every assertion below is empty.
case "$(realpath_of "$OLD_PREFIX")" in
  "$(realpath_of "$OLD_KEG")"/*)
    pass "the service runs the $OLD_VERSION keg's own venv"
    ;;
  *)
    die "the running api reports $OLD_PREFIX, not a path inside $OLD_KEG -- this job cannot tell two builds apart on this machine, so every assertion below would pass vacuously"
    ;;
esac

# ---------------------------------------------------------------------------
# 2. Stage the survivor: a process no service manager accounts for.
# ---------------------------------------------------------------------------
log "stage the pre-upgrade survivor (detached from launchd, as #4133's was)"
stage_survivor "$OLD_KEG" || die "could not stage the survivor"

IFS=$'\t' read -r STALE_PREFIX _ STALE_PID _ < <(runtime_report)
echo "  survivor prefix: $STALE_PREFIX (pid $STALE_PID)"
[ -n "$STALE_PID" ] || die "the running api reported no pid, so a mismatch could not name the process to stop"
pass "a nyxGPT api is serving that no service manager reports"

# ---------------------------------------------------------------------------
# 3. The upgrade, and the deletion that makes the next restart fatal.
# ---------------------------------------------------------------------------
log "brew upgrade $FORMULA $OLD_VERSION -> $NEW_VERSION"
# Kept so the survivor can be staged a SECOND time, for the second remediation
# (§7). One staging is consumed by whichever repair runs first, and both
# repairs have to be measured from the state they are prescribed for -- the
# review of #4155 found the printed remediation untested precisely because it
# ran only after the install step had already cleaned up. Copied before the
# upgrade and restored to the SAME absolute path, because a venv's `pyvenv.cfg`
# and console-script shebangs are absolute: a copy that runs has to run from
# where it was built.
KEG_BACKUP="$WORK/old-keg"
cp -R "$OLD_KEG" "$KEG_BACKUP" || die "could not back up $OLD_KEG for the second staging"
TAP_DIR="$(brew --repository "$TAP")"
cp "$NEW_RB" "$TAP_DIR/Formula/"
brew upgrade --verbose "$TAP/$FORMULA" 2>&1 | tail -30
test -d "$NEW_KEG" || die "$NEW_VERSION is not in the Cellar -- the upgrade did not happen"
pass "$NEW_VERSION is the installed keg"

# `brew cleanup` is what removes the superseded keg on the owner's machine
# (Homebrew does it automatically after its cleanup window). Doing it here
# reproduces the acute form: the running interpreter's venv is GONE, so the
# process is alive only until something restarts it.
brew cleanup "$FORMULA" 2>&1 | tail -10 || true
if [ -d "$OLD_KEG" ]; then
  # Not a failure of the product -- a property of this Homebrew's cleanup
  # policy. Remove it directly so the scenario under test is the owner's.
  echo "  brew left $OLD_KEG behind; removing it to reproduce the deleted-venv state"
  rm -rf "$OLD_KEG"
fi
test ! -d "$OLD_KEG" || die "could not remove $OLD_KEG"
pass "the venv the running process is executing no longer exists"

# #4133's literal headline, measured: "the next restart re-execs into a path
# that no longer exists and the api dies". The path is the one the survivor
# was started from.
if [ -x "$OLD_KEG/bin/nyxgpt-api" ]; then
  fail "$OLD_KEG/bin/nyxgpt-api still exists, so the fatal-restart half is not staged"
else
  pass "a restart of the survivor's own entrypoint would now fail: $OLD_KEG/bin/nyxgpt-api is gone"
fi

CODE="$(http_code "$API_URL/health" "$WORK/health.json")"
[ "$CODE" = "200" ] || die "the survivor stopped answering before the measurement -- it is supposed to keep serving from deleted files, which is the whole reason this state is invisible"
IFS=$'\t' read -r STALE_PREFIX _ STALE_PID STALE_EXISTS < <(runtime_report)
echo "  still serving: $STALE_PREFIX (pid $STALE_PID, prefix exists: $STALE_EXISTS)"
[ "$STALE_EXISTS" = "no" ] || fail "the api reports its own prefix as present, but $OLD_KEG is gone"

# The new keg's own python drives the measurements below: it is the build an
# operator now has, and the one whose code is under test.
KEG_PY="$NEW_KEG/libexec/venv/bin/python3"
test -x "$KEG_PY" || die "no venv python in $NEW_KEG -- the keg is not self-contained"
export PYTHONDONTWRITEBYTECODE=1

cat > "$WORK/drift_driver.py" <<'PY'
"""Report the running-build comparison, and optionally run the install step.

Calls the exact functions `nyxgpt ops install` and `nyxgpt ops status` call,
from inside the installed keg, against the live machine -- no mocks, no
reimplementation. `--reconcile` runs the install step itself.
"""
import json
import sys

from nyxgpt import ops

drift = ops._native_api_build_drift()
out = {"drift": drift.to_dict()}
if "--reconcile" in sys.argv:
    results = ops._reconcile_running_api_build()
    out["reconcile"] = [
        {"ok": r.ok, "status": r.status, "message": r.message, "details": r.details}
        for r in results
    ]
    out["after"] = ops._native_api_build_drift().to_dict()
print(json.dumps(out, indent=2))
PY

# Run the driver, keeping its JSON and its DIAGNOSTICS in separate files.
#
# Every call site used to be `> file.json 2>&1`, and then parsed `file.json`
# as JSON. `ops` logs to stderr, so any warning it emits while reconciling
# prepends a line to the "JSON" -- which is exactly what happened on
# 2026-10-09: two `Subprocess exited non-zero (rc=113): launchctl kickstart`
# warnings from self-heal landed at the top of `repair.json` and the parse
# died with `Expecting value: line 1 column 1`, with the `cat` above it
# showing a perfectly good-looking payload. The merge was there so a driver
# that dies has its traceback shown, which is worth keeping; what it must not
# do is corrupt the data. $1 is the JSON path, the rest are driver arguments.
run_driver() {
  local json="$1"; shift
  local log="${json%.json}.stderr.log"
  if ! "$KEG_PY" "$WORK/drift_driver.py" "$@" > "$json" 2> "$log"; then
    echo "--- driver stdout ---"; cat "$json"
    echo "--- driver stderr ---"; cat "$log"
    return 1
  fi
  # Shown, never merged: a warning here is diagnostic context for the
  # assertions below, not part of their input.
  if [ -s "$log" ]; then
    echo "  driver diagnostics (stderr):"
    sed 's/^/    /' "$log"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# 4. The defect, injected: revert the comparison in the installed keg.
#
# Pre-#4133 there was no comparison at all, so the closest faithful revert is
# a `same_tree` that always matches: every surface then reports the installed
# build and says nothing about the process. Measured, so a future green run of
# this job is evidence that the fix is what makes the difference rather than
# the runner failing to reproduce the bug.
# ---------------------------------------------------------------------------
log "injection: with the comparison reverted, the surfaces report [OK] over the stale process"
RB_MODULE="$("$KEG_PY" -c 'import nyxgpt.running_build as m; print(m.__file__)')"
echo "  keg module: $RB_MODULE"
cp "$RB_MODULE" "$WORK/running_build.shipped.py"
restore_the_keg() { cp "$WORK/running_build.shipped.py" "$RB_MODULE"; }
trap restore_the_keg EXIT

"$KEG_PY" - "$RB_MODULE" <<'PY'
"""Make `same_tree` an unconditional match, and prove the edit landed."""
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
needle = 'def same_tree(running: str, expected: str) -> bool:'
if needle not in source:
    raise SystemExit(f"::error::{needle!r} not found in {path} -- the injection would be a no-op")
patched = source.replace(needle, needle + "\n    return True  # injected (#4133)", 1)
path.write_text(patched, encoding="utf-8")
print("injected an unconditional match into same_tree")
PY
"$KEG_PY" -c 'from nyxgpt.running_build import same_tree; assert same_tree("/a", "/b"), "the injection did not take"' \
  || die "the injection did not change the keg's behavior, so half 1 would pass by looking like half 2"

run_driver "$WORK/injected.json" --reconcile \
  || die "the driver did not run under the injection"
cat "$WORK/injected.json"
INJECTED_STATE="$("$KEG_PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["drift"]["state"])' "$WORK/injected.json")"
if [ "$INJECTED_STATE" = "match" ]; then
  pass "reverted, the install step reports a match over a process running a deleted venv (the defect)"
else
  fail "the injection produced state '$INJECTED_STATE', not 'match' -- this half is not measuring the defect"
fi

nyxgpt ops status > "$WORK/status-injected.log" 2>&1 || true
if grep -q 'MISMATCH' "$WORK/status-injected.log"; then
  fail "ops status named a mismatch with the comparison reverted -- the injection did not reach the CLI"
else
  pass "reverted, nyxgpt ops status reports nothing wrong (what the owner saw)"
fi

log "restore the keg, and prove it is whole again"
restore_the_keg
trap - EXIT
"$KEG_PY" -c 'from nyxgpt.running_build import same_tree; assert not same_tree("/a", "/b"), "not restored"' \
  || die "the keg was not restored, so everything below measures injected code"
pass "same_tree discriminates again"

# ---------------------------------------------------------------------------
# 5. AC3: `nyxgpt ops status` states the mismatch as a mismatch.
# ---------------------------------------------------------------------------
log "nyxgpt ops status names the mismatch, both paths and the repair"
nyxgpt ops status > "$WORK/status.log" 2>&1 || true
sed -n '/Running api build/,/^$/p' "$WORK/status.log"
if grep -q 'MISMATCH' "$WORK/status.log"; then
  pass "status says MISMATCH"
else
  fail "nyxgpt ops status did not name the mismatch (AC3)"
fi
if grep -qF "$STALE_PREFIX" "$WORK/status.log"; then
  pass "status names the venv the process is actually running"
else
  fail "nyxgpt ops status did not print the running prefix $STALE_PREFIX"
fi
# The CELLAR path, deliberately. What the installed service execs is
# `<prefix>/opt/<formula>/libexec/venv` -- a symlink, by design, so that it
# follows a relink -- and that string reads identically before and after the
# upgrade this job just performed. So a `status` that named only it would
# answer "which build is installed?" with something that cannot tell rc0 from
# rc1, which is why the block now names what the symlink resolves to as well
# (#4182). This assertion is what holds that: it is the version-bearing half.
if grep -qF "$NEW_KEG" "$WORK/status.log"; then
  pass "status names the installed keg's venv, resolved to the version it is"
else
  sed -n '/Running api build/,/^$/p' "$WORK/status.log"
  fail "nyxgpt ops status did not print the installed prefix under $NEW_KEG"
fi
if grep -qF 'nyxgpt ops restart api' "$WORK/status.log"; then
  pass "status names the command that repairs it"
else
  fail "nyxgpt ops status did not name the repair command"
fi

# ---------------------------------------------------------------------------
# 6. AC1/AC2: the install step repairs it, and the live interpreter is the
#    NEW keg's -- verified by the process's own interpreter path.
# ---------------------------------------------------------------------------
log "the install step repairs the mismatch"
run_driver "$WORK/repair.json" --reconcile \
  || die "the reconcile step itself failed to run"
cat "$WORK/repair.json"

"$KEG_PY" - "$WORK/repair.json" <<'PY' || FAILURES=$((FAILURES + 1))
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
before, results, after = data["drift"], data["reconcile"], data["after"]
ok = True
if before["state"] != "mismatch":
    print(f"::error::the step saw state {before['state']!r}, not a mismatch -- nothing to repair")
    ok = False
failed = [r for r in results if not r["ok"]]
if failed:
    print(f"::error::the step reported failures: {failed}")
    ok = False
if after["state"] != "match":
    print(f"::error::after the repair the state is {after['state']!r}, not a match")
    ok = False
if ok:
    print("  [OK] the step saw a mismatch, repaired it, and now reports a match")
sys.exit(0 if ok else 1)
PY

CODE="$(wait_for_http "$API_URL/health" "$WORK/health.json" 240)"
[ "$CODE" = "200" ] || fail "the api is not serving after the repair (got $CODE)"
IFS=$'\t' read -r NEW_PREFIX NEW_PY NEW_PID NEW_EXISTS < <(runtime_report)
echo "  running prefix: $NEW_PREFIX (python $NEW_PY, pid $NEW_PID, prefix exists: $NEW_EXISTS)"

# AC1, stated the way the issue states it: verified by the process's own
# interpreter path, not by the reported version.
case "$(realpath_of "$NEW_PREFIX")" in
  "$(realpath_of "$NEW_KEG")"/*)
    pass "the live process is executing the $NEW_VERSION keg's venv (AC1)"
    ;;
  *)
    fail "the live process reports $NEW_PREFIX, which is not inside $NEW_KEG (AC1)"
    ;;
esac
if [ "$NEW_PID" != "$STALE_PID" ]; then
  pass "the stale process (pid $STALE_PID) is no longer the one serving"
else
  fail "pid $STALE_PID is still serving -- the repair did not replace the process"
fi
if kill -0 "$STALE_PID" 2>/dev/null; then
  fail "pid $STALE_PID is still alive after the repair"
else
  pass "the stale process is gone"
fi

# ---------------------------------------------------------------------------
# 7. AC4, and the finding from the review of #4155: the remediation every
#    surface PRINTS has to repair the state it is printed for.
#
#    The survivor is staged a second time, deliberately, because the install
#    step in step 6 consumed the first one. Measuring `nyxgpt ops restart api`
#    only on an already-healthy stack is what let a bare `brew services
#    restart` -- which acts on the REGISTERED service, and nothing is
#    registered here -- look like a working repair: launchd would start the new
#    build onto a port the survivor still holds, uvicorn would fail to bind
#    (`[Errno 48]`, #3853's restart-loop shape), and the command would exit
#    `[OK]` over a mismatch that never moved.
# ---------------------------------------------------------------------------
log "re-stage the survivor, so the printed remediation is measured from the state it names"
stage_survivor "$OLD_KEG" || die "could not re-stage the survivor for the remediation half"
# Deleted again while the process runs: the acute form, same as step 3.
rm -rf "$OLD_KEG"
test ! -d "$OLD_KEG" || die "could not remove $OLD_KEG for the second staging"
IFS=$'\t' read -r STALE2_PREFIX _ STALE2_PID STALE2_EXISTS < <(runtime_report)
echo "  survivor prefix: $STALE2_PREFIX (pid $STALE2_PID, prefix exists: $STALE2_EXISTS)"
[ -n "$STALE2_PID" ] || die "the re-staged survivor reported no pid"
[ "$STALE2_EXISTS" = "no" ] || fail "the re-staged survivor reports its own prefix as present, but $OLD_KEG is gone"

# Non-vacuity, asserted before the remediation runs: if the machine is not in
# a mismatch state here, the assertions below pass without measuring anything.
run_driver "$WORK/restage.json" \
  || die "the driver did not run against the re-staged survivor"
RESTAGED_STATE="$("$KEG_PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["drift"]["state"])' "$WORK/restage.json")"
[ "$RESTAGED_STATE" = "mismatch" ] \
  || die "the re-staged machine reports state '$RESTAGED_STATE', not 'mismatch' -- the remediation below would have nothing to repair"
pass "the machine is back in the mismatch state the surfaces print a remediation for"

log "nyxgpt ops restart api repairs it, from the staged state (AC4)"
nyxgpt ops restart api > "$WORK/restart.log" 2>&1
RESTART_RC=$?
tail -30 "$WORK/restart.log"
# The command must not report success over a persisting mismatch. Checked
# alongside the state below rather than instead of it: an exit code is a
# report, the interpreter path is the evidence.
if [ "$RESTART_RC" -ne 0 ]; then
  fail "nyxgpt ops restart api exited $RESTART_RC from the staged state"
else
  pass "nyxgpt ops restart api reported success"
fi
if grep -qF 'no service manager accounts for' "$WORK/restart.log"; then
  pass "the restart named the unaccounted-for process it had to stop first"
else
  fail "nyxgpt ops restart api did not report stopping the unmanaged survivor -- a bare service restart cannot repair this state"
fi
CODE="$(wait_for_http "$API_URL/health" "$WORK/health.json" 240)"
[ "$CODE" = "200" ] || fail "the api did not come back after a restart (got $CODE) -- AC4"
IFS=$'\t' read -r RESTART_PREFIX _ RESTART_PID _ < <(runtime_report)
echo "  running prefix: $RESTART_PREFIX (pid $RESTART_PID)"
case "$(realpath_of "$RESTART_PREFIX")" in
  "$(realpath_of "$NEW_KEG")"/*)
    pass "after the printed remediation the api runs the $NEW_VERSION keg's venv (AC4)"
    ;;
  *)
    fail "after nyxgpt ops restart api the api reports $RESTART_PREFIX, not the $NEW_VERSION keg (AC4)"
    ;;
esac
if kill -0 "$STALE2_PID" 2>/dev/null; then
  fail "pid $STALE2_PID is still alive after the printed remediation -- it repaired nothing"
else
  pass "the re-staged survivor is gone"
fi
run_driver "$WORK/after-restart.json" || true
AFTER_STATE="$("$KEG_PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["drift"]["state"])' "$WORK/after-restart.json" 2>/dev/null || echo unreadable)"
if [ "$AFTER_STATE" = "match" ]; then
  pass "the comparison now reports a match"
else
  fail "after the printed remediation the comparison still reports '$AFTER_STATE'"
fi

log "teardown"
brew services stop "$FORMULA" 2>&1 | tail -5 || true

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
  echo "macOS upgrade smoke: every assertion passed"
  exit 0
fi
echo "macOS upgrade smoke: $FAILURES assertion(s) failed"
exit 1
