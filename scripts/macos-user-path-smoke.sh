#!/usr/bin/env bash
#
# The macOS user path, executed (#3860).
#
# `macos-brew-smoke.yml` used to prove that a keg builds. It never proved that
# a person can use one: neither install job ran `nyxgpt`, started the stack,
# issued an HTTP request or removed anything. Six defects on the certified
# path (#3850 no CLI on PATH, #3851 a 500 from the session list, #3853 a
# health probe that cannot see its own services, #3854 caveats pointing at the
# wrong command, #3857 a web UI stuck on placeholders, #3859 a teardown that
# leaves services running) reached owner acceptance over green runs of that
# gate, and the Phase 6 capstone (#3516) closed on the same shape of evidence.
#
# This script is the missing half: from an installed candidate it runs the
# owner's literal sequence -- version, up, API, session list, web UI, status,
# down, uninstall, untap -- and asserts the answer at every step. It lives in
# the repository rather than inline in the workflow so the same sequence can be
# run by hand on a real Mac (`./scripts/macos-user-path-smoke.sh 3.0.0rc12
# dkblinux98/nyxgpt`) and so `tests/unit/test_macos_user_path_smoke.py` can
# pin what it asserts.
#
# Two rules keep it from decaying back into what it replaced:
#
#   1. Every probe is asserted, and a failure is recorded rather than aborting,
#      so one broken step cannot hide the state of the rest. The exit code is
#      non-zero if any assertion failed.
#   2. The only tolerated failures are the `nyxgpt up` steps a hosted macOS
#      runner physically cannot run -- the Docker-backed ones. That list is
#      $TOLERATED_STEPS below and it is mirrored in
#      docs/live-verification-ci.md. Anything else failing is a defect on the
#      user path. Widening this list is how a gate goes hollow; if a step
#      genuinely cannot run here, say so in that document in the same commit.
#
# Usage: macos-user-path-smoke.sh <candidate version> <tap>
#   e.g. macos-user-path-smoke.sh 3.0.0rc12 dkblinux98/nyxgpt

set -uo pipefail

VERSION="${1:-}"
TAP="${2:-}"
if [ -z "$VERSION" ] || [ -z "$TAP" ]; then
  echo "usage: $0 <candidate version, e.g. 3.0.0rc12> <tap, e.g. dkblinux98/nyxgpt>" >&2
  exit 64
fi

RELEASE="${VERSION%%rc*}"
API_FORMULA="nyxgpt-api@${RELEASE}rc"

API_URL="http://127.0.0.1:8000"
WEB_URL="http://127.0.0.1:3000"

# `nyxgpt up` steps whose failure is a property of this machine, not of the
# product: every one of them needs a Docker daemon, and the hosted macOS
# images ship none (Docker Desktop is a licensed GUI app, and the Apple
# Silicon runners expose no nested virtualisation, so Colima cannot stand in).
# Named in docs/live-verification-ci.md as owner-acceptance-only. The
# api/web/ollama services, the config, the install-mode record and the env
# sync are deliberately NOT here -- those are the user path.
TOLERATED_STEPS='docker engine,cassandra container,cassandra log follower service,observability stack,glitchtip secrets dir,glitchtip auto-provisioning,slack webhook secret'

WORK="$(mktemp -d)"
FAILURES=0

log() { printf '\n=== %s ===\n' "$*"; }

pass() { printf '  [OK] %s\n' "$*"; }

fail() {
  printf '  [FAIL] %s\n' "$*"
  echo "::error::$*"
  FAILURES=$((FAILURES + 1))
}

# `curl` writes the body to $2 and prints the status code. A connection that
# never opens makes curl print 000 and exit non-zero, so the exit code is
# deliberately ignored: 000 is the answer this script wants for "nothing is
# listening" (which is a pass after teardown and a failure before it), and
# appending a second 000 in an `|| echo` would corrupt every comparison.
http_code() {
  local code
  code="$(curl -s -o "$2" -w '%{http_code}' --max-time 15 "$1" 2>/dev/null)"
  echo "${code:-000}"
}

# Poll `url` until it answers 200 or `deadline` seconds elapse. A service that
# is starting is not a failure; a service that never answers is.
wait_for_http() {
  local url="$1" body="$2" seconds="${3:-120}"
  local deadline=$((SECONDS + seconds)) code="000"
  while [ "$SECONDS" -lt "$deadline" ]; do
    code="$(http_code "$url" "$body")"
    [ "$code" = "200" ] && break
    sleep 3
  done
  echo "$code"
}

# ---------------------------------------------------------------------------
# Precondition: this runner can actually run services.
#
# Asserted rather than detected-and-skipped. Skipping the service checks when
# the launchd domain is unavailable would reproduce exactly the failure this
# script exists to stop -- a job that reports green while asserting nothing
# about the product. If a hosted runner ever stops supporting `brew services`,
# that boundary belongs in docs/live-verification-ci.md as a named
# owner-acceptance gap, and this script should fail loudly until it is written
# there.
# ---------------------------------------------------------------------------
log "precondition: brew services is usable on this machine"
if ! brew services list > "$WORK/services-precheck.log" 2>&1; then
  cat "$WORK/services-precheck.log"
  echo "::error::brew services is not usable on this runner, so nothing below would measure the product."
  echo "Do not weaken this script to get past it: record the boundary in docs/live-verification-ci.md"
  echo "as an owner-acceptance gap, with the evidence, and fix the gate deliberately."
  exit 1
fi
cat "$WORK/services-precheck.log"
pass "brew services answers"

# Everything below runs from $HOME, never from a checkout. The published-tap
# job clones this repository to get this script, and a command run from inside
# that clone can resolve repo-relative paths that a real install does not have
# (#3759's defect class). The product is exercised the way a user has it: from
# a home directory, with no repository anywhere above it.
cd "$HOME" || exit 1

# ---------------------------------------------------------------------------
# 1. The command exists, by name (#3850).
# ---------------------------------------------------------------------------
log "nyxgpt --version"
if ! command -v nyxgpt > "$WORK/which.log" 2>&1; then
  fail "nyxgpt is not on PATH after brew install -- the 'one command' does not exist (#3850)"
else
  cat "$WORK/which.log"
  if nyxgpt --version; then
    pass "the CLI runs by name"
  else
    fail "nyxgpt --version failed"
  fi
fi

# ---------------------------------------------------------------------------
# 2. A config, without a TTY.
#
# `nyxgpt ops install` launches the interactive wizard on a machine with no
# config and refuses (correctly) when stdin is not a TTY. The wizard itself is
# owner-acceptance territory -- it prompts -- so seed the config the way a
# headless install must, from the copy the *installed package* carries. That
# read is itself an assertion: an artifact install that lost its packaged
# resources fails here rather than three steps later.
# ---------------------------------------------------------------------------
log "seed ~/.nyxGPT/config.ini from the installed package's own resources"
KEG_PY="$(brew --prefix "$API_FORMULA")/libexec/venv/bin/python3"
if [ ! -x "$KEG_PY" ]; then
  fail "no venv python in the $API_FORMULA keg -- the install did not complete"
else
  if "$KEG_PY" - <<'PY'; then
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
    pass "config.ini in place"
  else
    fail "the installed package could not produce its own example.config.ini (#3759 class)"
  fi
fi

# ---------------------------------------------------------------------------
# 3. The single command of the capstone scenario (#3516).
#
# Its exit code is not the assertion: the Docker-backed steps genuinely cannot
# succeed here. What IS asserted is that every step outside $TOLERATED_STEPS
# succeeded -- so an api/web/ollama service that fails to install or start
# fails this script, which is the half of `up` this runner can measure.
# ---------------------------------------------------------------------------
log "nyxgpt up"
nyxgpt up --skip-observability --timeout 300 2>&1 | tee "$WORK/up.log"
UP_RC="${PIPESTATUS[0]}"
echo "nyxgpt up exited $UP_RC"

TOLERATED_STEPS="$TOLERATED_STEPS" python3 - "$WORK/up.log" <<'PY' > "$WORK/up-verdict.txt"
"""Attribute every [FAIL] line to the `[n/m] step...` banner above it.

`nyxgpt ops install` streams a banner per step and then that step's own
OK/FAIL results, so the banner is the only thing that says which step a
failure belongs to. Anything failing outside the tolerated set is reported
for the shell to turn into an assertion.
"""
import os
import re
import sys

tolerated = {s.strip() for s in os.environ["TOLERATED_STEPS"].split(",") if s.strip()}
banner = re.compile(r"^\[\d+/\d+\] (?P<step>.+?)\.\.\.\s*$")

step = "(before the first step)"
offenders: dict[str, list[str]] = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.rstrip("\n")
    match = banner.match(line)
    if match:
        step = match.group("step")
        continue
    if line.startswith("[FAIL]") and step not in tolerated:
        offenders.setdefault(step, []).append(line)

for step, lines in offenders.items():
    print(f"UNTOLERATED\t{step}\t{lines[0]}")
print(f"TOLERATED_SET\t{', '.join(sorted(tolerated))}")
PY

cat "$WORK/up-verdict.txt"
if grep -q '^UNTOLERATED' "$WORK/up-verdict.txt"; then
  while IFS=$'\t' read -r _ step first; do
    fail "nyxgpt up step '$step' failed on the user path: $first"
  done < <(grep '^UNTOLERATED' "$WORK/up-verdict.txt")
else
  pass "every nyxgpt up step outside the documented Docker-only set succeeded"
fi

# ---------------------------------------------------------------------------
# 4. The API answers (#3853).
# ---------------------------------------------------------------------------
log "GET $API_URL/health"
CODE="$(wait_for_http "$API_URL/health" "$WORK/health.json" 180)"
head -c 400 "$WORK/health.json"; echo
if [ "$CODE" = "200" ]; then
  pass "the API answers /health with 200"
else
  fail "GET /health answered $CODE, not 200 -- the installed API is not serving (#3853)"
fi

# ---------------------------------------------------------------------------
# 5. The session list answers, and does not 500 (#3851).
#
# A stack that starts but cannot reach its datastore passes every check that
# only asks whether a process is up. This one asks the datastore a question.
# ---------------------------------------------------------------------------
log "GET $API_URL/api/v1/sessions"
CODE="$(wait_for_http "$API_URL/api/v1/sessions" "$WORK/sessions.json" 60)"
head -c 400 "$WORK/sessions.json"; echo
if [ "$CODE" = "200" ]; then
  pass "the session list answers 200"
else
  fail "GET /api/v1/sessions answered $CODE, not 200 -- the stack cannot reach its session store (#3851)"
fi

# ---------------------------------------------------------------------------
# 6. The web UI serves (#3857).
#
# 200 plus real markup: the placeholder-forever dashboard still returns a
# document, so the status code alone is not the whole answer. Next.js streams
# the shell first, so this asserts the document is an HTML document the app
# produced, not that a particular panel rendered -- panel content is owner
# acceptance (a browser), and is named as such in docs/live-verification-ci.md.
# ---------------------------------------------------------------------------
log "GET $WEB_URL/"
CODE="$(wait_for_http "$WEB_URL/" "$WORK/web.html" 300)"
head -c 400 "$WORK/web.html"; echo
if [ "$CODE" = "200" ]; then
  pass "the web UI answers / with 200"
else
  fail "GET $WEB_URL/ answered $CODE, not 200 -- the web service is not serving (#3857)"
fi
if grep -qi '<!DOCTYPE html' "$WORK/web.html"; then
  pass "the web UI served an HTML document"
else
  fail "the web UI's response is not an HTML document (#3857)"
fi

# ---------------------------------------------------------------------------
# 7. What the operator is told is running (#3854).
#
# `nyxgpt ops status` is documented to always exit 0, so a non-zero exit here
# is the artifact path breaking. Its *content* matters too: the services this
# very script just started have to appear in it, or the operator's only
# read-out disagrees with their machine.
# ---------------------------------------------------------------------------
log "nyxgpt ops status"
if nyxgpt ops status 2>&1 | tee "$WORK/status.log"; then
  pass "nyxgpt ops status exited 0"
else
  fail "nyxgpt ops status exited non-zero on an installed machine"
fi
# The line shape is the one `ops.py` actually prints -- `  native  api: started`,
# `  compose web: running`, `  terraform api: running` (src/nyxgpt/ops.py, the
# "Deployment mode:" block). No status line ever begins with a bare component
# name, so a `^[[:space:]]*api\b` pattern could not match even a perfectly
# working install; it reported a phantom #3854 forever.
#
# And naming the component is not enough on its own: `native  api: none` names
# it while telling the operator nothing is there. This asserts the state as
# well, because that is the read-out #3854 is about -- what the operator is
# told about the services this script just started.
for component in api web; do
  state_line="$(grep -iE "^[[:space:]]*(native|compose|terraform)[[:space:]]+${component}:" \
    "$WORK/status.log" | head -1 || true)"
  if [ -n "$state_line" ]; then
    pass "status names the $component component ($(printf '%s' "$state_line" | sed 's/^[[:space:]]*//'))"
  else
    fail "nyxgpt ops status never mentions $component, so the operator cannot see what they installed (#3854)"
    continue
  fi
  # `started` is what `brew services list` reports for a running native
  # service and what the native snapshot passes straight through; `running`
  # covers the Compose/Terraform spellings of the same state.
  if printf '%s' "$state_line" | grep -qiE ":[[:space:]]*(started|running)\b"; then
    pass "status reports $component as running"
  else
    fail "nyxgpt ops status reports $component as not running ($(printf '%s' "$state_line" | sed 's/^[[:space:]]*//')) after nyxgpt up (#3854)"
  fi
done

log "nyxgpt self-heal status"
nyxgpt self-heal status 2>&1 | tee "$WORK/self-heal.log"
for component in api web; do
  if grep -qE "^ \[OK\] ${component}:" "$WORK/self-heal.log"; then
    pass "the health probe sees $component"
  else
    fail "the health probe cannot see the $component service it installed, so nyxgpt up can never return 0 (#3853)"
  fi
done

# ---------------------------------------------------------------------------
# 8. Teardown (#3859).
#
# The wrapped stop first, then the Homebrew removal, then the assertion that
# nothing is left running or registered. Uninstalling a keg while its launchd
# job is loaded is precisely the state the owner was left in.
# ---------------------------------------------------------------------------
log "nyxgpt down"
if nyxgpt down 2>&1 | tee "$WORK/down.log"; then
  pass "nyxgpt down exited 0"
else
  fail "nyxgpt down exited non-zero -- the supported stop does not work (#3859)"
fi

log "brew uninstall / brew untap"
# Everything named nyxgpt, not just the two candidate formulas this script
# installed: `nyxgpt up` installs the api/web services itself, and on the rc
# channel it resolves the *stable* formula names (`_install_from_remote_tap`
# derives the formula from the installed package's own version, which is the
# project version, not the candidate stamp). Removing only what this script
# asked for would leave those behind and turn the untap below into a failure
# that reads as a teardown defect when it is really #3853. Remove what is
# actually on the machine, and say what that was.
#
# Newline-separated strings rather than arrays throughout: macOS still ships
# bash 3.2 as /bin/bash, `mapfile` does not exist there, and expanding an
# empty array under `set -u` is an error in that version.
INSTALLED="$(brew list --formula 2>/dev/null | grep -i '^nyxgpt' || true)"
echo "installed nyxgpt formulas: ${INSTALLED:-none}"
if [ -z "$INSTALLED" ]; then
  fail "no nyxgpt formula is installed at teardown time -- the install did not survive the run"
fi
while IFS= read -r formula; do
  [ -n "$formula" ] || continue
  if brew uninstall --force "$formula" 2>&1 | tail -20; then
    pass "brew uninstall removed $formula"
  else
    fail "brew uninstall $formula failed (#3859)"
  fi
done <<< "$INSTALLED"
STILL_INSTALLED="$(brew list --formula 2>/dev/null | grep -i '^nyxgpt' || true)"
if [ -n "$STILL_INSTALLED" ]; then
  fail "still installed after uninstall: $(echo "$STILL_INSTALLED" | tr '\n' ' ') (#3859)"
fi
if brew untap "$TAP" 2>&1 | tail -20; then
  pass "brew untap removed the tap"
else
  fail "brew untap $TAP failed (#3859)"
fi

log "residue after uninstall"
launchctl list > "$WORK/launchctl.log" 2>&1 || true
if grep -i nyxgpt "$WORK/launchctl.log"; then
  fail "a nyxgpt launchd job is still loaded after uninstall (#3859)"
else
  pass "no nyxgpt job left in launchctl list"
fi

# A glob rather than `ls | grep`: `nocaseglob` covers both spellings the
# machine uses (`homebrew.mxcl.nyxgpt-api.plist` from brew services,
# `com.nyxgpt.*.plist` from the ops-installed LaunchAgents).
shopt -s nullglob nocaseglob
LEFTOVER_PLISTS=""
for plist in "$HOME"/Library/LaunchAgents/*nyxgpt*; do
  LEFTOVER_PLISTS="${LEFTOVER_PLISTS}${plist}"$'\n'
done
shopt -u nullglob nocaseglob
if [ -n "$LEFTOVER_PLISTS" ]; then
  printf '%s' "$LEFTOVER_PLISTS"
  fail "nyxgpt plists remain in ~/Library/LaunchAgents after uninstall (#3859)"
else
  pass "no nyxgpt plist left in ~/Library/LaunchAgents"
fi

for port_url in "$API_URL/health" "$WEB_URL/"; do
  CODE="$(http_code "$port_url" "$WORK/after.log")"
  if [ "$CODE" = "000" ]; then
    pass "$port_url stopped answering after teardown"
  else
    fail "$port_url still answers $CODE after uninstall -- services survive removal (#3859)"
  fi
done

log "verdict"
if [ "$FAILURES" -ne 0 ]; then
  echo "$FAILURES assertion(s) failed on the macOS user path."
  exit 1
fi
echo "the macOS user path installs, runs, serves and tears down cleanly."
