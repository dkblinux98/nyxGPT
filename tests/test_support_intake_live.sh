#!/usr/bin/env bash
# Executed verification for the in-app support intake (#3811, D-006).
#
# Which question does this answer?
#
#   "If a user fills in the Support form and presses File ticket, does a
#    correctly-labeled ticket actually leave this machine -- and does the
#    install notice when GitHub silently drops the label?"
#
# Inspection cannot answer either half. The first is a claim about a running
# API: the request is assembled from config, an environment probe and an
# httpx call, none of which a reviewer can execute by reading. The second is
# the #3810 failure mode from the other side -- GitHub accepts an issue from
# a token without push access and DROPS `labels` without erroring, so an
# implementation that assumes the label applied looks identical, from the
# outside, to one that checks. Only running both cases separates them.
#
# So this starts the REAL API (uvicorn, the shipped app) against a stub
# GitHub, files real tickets through real HTTP, and inspects what arrived at
# the other end. The stub is why this can run on every push: filing against
# api.github.com would put a throwaway ticket in the live Support project
# each time.
#
# Four cases, three of them injected faults:
#   1. the happy path -- a labeled, form-shaped ticket leaves the machine
#   2. GitHub drops the label -> the response says `labeled: false`
#      (the case an assume-it-worked implementation cannot distinguish)
#   3. no credential configured -> 503 carrying the prefilled GitHub form
#   4. GitHub refuses -> 502 with a sentence for the filer, not a traceback
#
# Usage: tests/test_support_intake_live.sh
# Requires: the package installed (`pip install -e .`), curl, python3.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
WORK="$(mktemp -d)"
CONFIG_DIR="$HOME/.nyxGPT"
CONFIG_PATH="$CONFIG_DIR/config.ini"
CONFIG_BACKUP="$WORK/config.ini.backup"
STUB_PORT="${STUB_PORT:-8931}"
API_PORT="${API_PORT:-8901}"
STUB_PID=""
API_PID=""
CHECKS=0

pass() { CHECKS=$((CHECKS + 1)); echo "  ok: $1"; }
fail() { echo "::error::$1"; exit 1; }

cleanup() {
  [[ -n "$API_PID" ]] && kill "$API_PID" 2>/dev/null || true
  [[ -n "$STUB_PID" ]] && kill "$STUB_PID" 2>/dev/null || true
  # Put back whatever config was here. This script writes the real
  # `~/.nyxGPT/config.ini` because that is the only path `load_config`
  # reads, so it has to be able to undo that.
  if [[ -f "$CONFIG_BACKUP" ]]; then
    cp "$CONFIG_BACKUP" "$CONFIG_PATH"
  else
    rm -f "$CONFIG_PATH"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

# --- The stub GitHub -----------------------------------------------------
#
# Records what the intake sent and answers however the case needs. Modes:
#   labeled   -- behaves like a token WITH push access (labels echoed back)
#   unlabeled -- behaves like a token WITHOUT push access (labels dropped,
#                no error), which is exactly what #3810 looked like
#   refuse    -- answers 403

cat > "$WORK/stub_github.py" <<'PY'
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = os.environ["STUB_MODE"]
CAPTURE = os.environ["STUB_CAPTURE"]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(body)
        with open(CAPTURE, "w", encoding="utf-8") as fh:
            json.dump({"path": self.path, "headers": dict(self.headers), "body": payload}, fh)

        if MODE == "refuse":
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"message": "Resource not accessible"}')
            return

        labels = payload.get("labels", []) if MODE == "labeled" else []
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "number": 4321,
                    "html_url": "https://github.com/dkblinux98/nyxGPT/issues/4321",
                    "labels": [{"name": name} for name in labels],
                }
            ).encode()
        )

    def log_message(self, *_args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY

start_stub() {
  local mode="$1"
  [[ -n "$STUB_PID" ]] && { kill "$STUB_PID" 2>/dev/null || true; wait "$STUB_PID" 2>/dev/null || true; }
  rm -f "$WORK/captured.json"
  STUB_MODE="$mode" STUB_CAPTURE="$WORK/captured.json" \
    "$PYTHON" "$WORK/stub_github.py" "$STUB_PORT" &
  STUB_PID=$!
  wait_for "http://127.0.0.1:$STUB_PORT" "the stub GitHub"
}

wait_for() {
  local url="$1" what="$2" i
  for i in $(seq 1 60); do
    if curl -s -o /dev/null "$url" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  fail "$what never came up at $url"
}

write_config() {
  local pat="$1"
  mkdir -p "$CONFIG_DIR"
  cat > "$CONFIG_PATH" <<EOF
[ollama]
base_url = http://127.0.0.1:11434

[nyxgpt]
default_model = qwen2.5-coder:latest

[github]
pat = $pat

[logging]
dir = $WORK/logs
EOF
}

start_api() {
  [[ -n "$API_PID" ]] && { kill "$API_PID" 2>/dev/null || true; wait "$API_PID" 2>/dev/null || true; }
  (
    cd "$REPO_ROOT"
    NYXGPT_GITHUB_API_BASE="http://127.0.0.1:$STUB_PORT" \
      "$PYTHON" -m uvicorn nyxgpt.app:app --host 127.0.0.1 --port "$API_PORT" \
      > "$WORK/api.log" 2>&1
  ) &
  API_PID=$!
  wait_for "http://127.0.0.1:$API_PORT/api/v1/support/context" "the nyxGPT API"
}

file_ticket() {
  curl -s -o "$WORK/response.json" -w '%{http_code}' \
    -X POST "http://127.0.0.1:$API_PORT/api/v1/support/tickets" \
    -H 'Content-Type: application/json' \
    -d '{"ticket_type": "Bug Found", "summary": "Docs are a mess",
         "description": "I cannot find the install steps."}'
}

jq_get() { "$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))$2)" "$1"; }

echo "== Support intake, executed against a running API =="

[[ -f "$CONFIG_PATH" ]] && cp "$CONFIG_PATH" "$CONFIG_BACKUP"

# --- 1. The happy path ---------------------------------------------------

write_config "smoke-token-not-a-real-credential"
start_stub labeled
start_api

STATUS="$(file_ticket)"
[[ "$STATUS" == "201" ]] || fail "filing answered HTTP $STATUS: $(cat "$WORK/response.json")"
pass "a ticket filed through the running API is created (HTTP 201)"

[[ "$(jq_get "$WORK/response.json" "['url']")" == *"/issues/4321" ]] \
  || fail "the response carries no link to the created ticket"
pass "the filer gets a link to their own ticket, not a promise"

[[ "$(jq_get "$WORK/response.json" "['labeled']")" == "True" ]] \
  || fail "the intake did not confirm the Support label"
pass "the Support label is confirmed on the created issue"

[[ -f "$WORK/captured.json" ]] || fail "nothing reached GitHub at all"
[[ "$(jq_get "$WORK/captured.json" "['body']['labels']")" == "['Support']" ]] \
  || fail "the request GitHub received carried no Support label -- the #3810 leak"
pass "the request that left this machine carries labels: ['Support']"

BODY="$(jq_get "$WORK/captured.json" "['body']['body']")"
[[ "$BODY" == *"### Installed version"* ]] \
  || fail "the ticket body is not form-shaped; support_intake_guard.yml would not recognise it"
[[ "$BODY" == *"### Ticket type"* && "$BODY" == *"Bug Found"* ]] \
  || fail "the ticket type the filer chose is not in the body"
pass "the body is form-shaped, and carries the type the filer chose"

[[ "$(jq_get "$WORK/captured.json" "['body']['title']")" == "support: Docs are a mess" ]] \
  || fail "the title lost its support: prefix"
pass "the title is prefixed exactly once"

# --- 2. Fault injection: GitHub drops the label --------------------------
#
# The half that cannot be inspected. Same request, same code path -- the only
# difference is what GitHub says back, and an implementation that assumed the
# label applied would answer identically to case 1 here.

start_stub unlabeled
STATUS="$(file_ticket)"
[[ "$STATUS" == "201" ]] || fail "a dropped label must not fail the filing (HTTP $STATUS)"
[[ "$(jq_get "$WORK/response.json" "['labeled']")" == "False" ]] \
  || fail "WITHOUT the read-back: a ticket GitHub left UNLABELED is reported as labeled. \
That is #3810 -- the ticket routes nowhere, the agent-loop skip never fires, and nothing says so."
pass "a label GitHub dropped is detected and reported, not assumed"

# The operator's evidence, wherever this install's logging puts it: the API's
# own stream, or the configured log dir.
grep -rq "WITHOUT the 'Support' label" "$WORK/api.log" "$WORK/logs" \
  || fail "the operator's logs say nothing about the unrouted ticket"
pass "the degraded case is in the API logs for the operator"

# --- 3. Fault injection: no credential -----------------------------------

write_config ""
start_api
STATUS="$(file_ticket)"
[[ "$STATUS" == "503" ]] || fail "a tokenless install answered HTTP $STATUS, expected 503"
[[ "$(jq_get "$WORK/response.json" "['issue_form_url']")" == *"template=support.yml"* ]] \
  || fail "a tokenless install offers no way to report anything at all"
pass "an install with no credential answers 503 with the prefilled GitHub form"

[[ "$(curl -s "http://127.0.0.1:$API_PORT/api/v1/support/context" \
      | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["can_submit"])')" == "False" ]] \
  || fail "the context claims this install can file when it cannot"
pass "the menu is told, in advance, that this install cannot file"

# --- 4. Fault injection: GitHub refuses ----------------------------------

write_config "smoke-token-not-a-real-credential"
start_stub refuse
start_api
STATUS="$(file_ticket)"
[[ "$STATUS" == "502" ]] || fail "a GitHub refusal answered HTTP $STATUS, expected 502"
RESPONSE="$(cat "$WORK/response.json")"
[[ "$RESPONSE" == *"rate limit"* ]] \
  || fail "the refusal reached the filer as something other than a readable sentence: $RESPONSE"
[[ "$RESPONSE" != *"Traceback"* ]] || fail "a traceback reached the filer"
pass "a GitHub refusal becomes a sentence about the ticket, not a 500"

echo "== $CHECKS checks passed =="
