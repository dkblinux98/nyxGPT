#!/usr/bin/env bash
# Executed proof that the retrospective dashboard stamps itself (#3807).
#
# Which question does this answer?
#
#   "Does a built retro.html actually SHOW when it was built, how old each
#    dump behind it is, and does the staleness callout appear only when the
#    data really is older than the page?"
#
# The stamps are rendered by the template's JavaScript at page load, so a
# Python test can prove the numbers reached the HTML but not that a reader
# ever sees them — a template typo blanks the page silently. This builds the
# real dashboard from a data directory whose stamps are injected (one dump
# fresh, one 30 days behind, the corpus unstamped), then executes the page
# script in Node and asserts on the rendered strings. A run where every source
# happened to be fresh would prove nothing, so both halves are injected.
#
# Usage: bash tests/test_retro_dashboard_stamps.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RETRO="$REPO_ROOT/scripts/retrospective"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0
check() { # check <description> <haystack> <needle>
  if [[ "$2" == *"$3"* ]]; then
    echo "  ok: $1"
    pass=$((pass + 1))
  else
    echo "  FAIL: $1"
    echo "        expected to find: $3"
    fail=$((fail + 1))
  fi
}
refute() { # refute <description> <haystack> <needle>
  if [[ "$2" != *"$3"* ]]; then
    echo "  ok: $1"
    pass=$((pass + 1))
  else
    echo "  FAIL: $1"
    echo "        expected NOT to find: $3"
    fail=$((fail + 1))
  fi
}

cp -R "$RETRO/data" "$WORK/data"

# Fault injection: spend is refreshed in this pass, churn is a month behind,
# and the issue corpus carries no stamp at all.
python3 - "$WORK/data" <<'PY'
import json, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

data = Path(sys.argv[1])
now = datetime.now(UTC)


def stamp(name, when):
    path = data / name
    payload = json.loads(path.read_text())
    payload["generated_at"] = when.isoformat()
    path.write_text(json.dumps(payload))


stamp("spend.json", now)
stamp("churn.json", now - timedelta(days=30))
stamp("relationships.json", now)
stamp("project_fields.json", now)
stamp("dashboard_data.json", now)
# all_issues.json must be UNSTAMPED for this phase -- that is the whole point
# of it. The old code *assumed* it (the corpus used to ship as a bare list),
# and that assumption silently stopped holding when the dump began writing
# {"generated_at", "issues"}: the fixture was stamped, so "an unstamped corpus
# renders as unknown" could not be exercised at all. Make it so instead of
# hoping it is.
_ai = data / "all_issues.json"
_raw = json.loads(_ai.read_text())
_ai.write_text(json.dumps(_raw.get("issues", []) if isinstance(_raw, dict) else _raw))
PY

echo "== build with an unstamped corpus, a fresh dump and a 30-day-old dump"
python3 "$RETRO/build_dashboard.py" \
  --data-dir "$WORK/data" --template "$RETRO/retro_template.html" --out "$WORK/a.html" \
  | tee "$WORK/a.log"
rendered_a="$(node "$REPO_ROOT/tests/retro_render_check.mjs" "$WORK/a.html")"

check "builder reports the unstamped corpus" "$(cat "$WORK/a.log")" \
  "unstamped sources: all_issues.json"
check "builder reports the stale dump" "$(cat "$WORK/a.log")" "stale sources: churn.json"
check "header carries a build time" "$rendered_a" "built "
check "header build time has time granularity, not date only" "$rendered_a" ":"
check "header flags the sources behind it" "$rendered_a" "stale or unstamped"
check "unstamped corpus says unknown, not nothing" "$rendered_a" \
  "Issue corpus as of unknown"
check "the unknown names the file that lacks the stamp" "$rendered_a" \
  "all_issues.json carries no generated_at stamp"
check "the 30-day-old dump is called out where it is presented" "$rendered_a" \
  "30 days older than this page"
spend_line="$(printf '%s' "$rendered_a" | python3 -c 'import json,sys; print(json.load(sys.stdin)["asof"]["spend"])')"
check "the dump refreshed in this pass is shown with its own time" "$spend_line" \
  "Spend telemetry as of"
refute "the dump refreshed in this pass is NOT called out as stale" "$spend_line" \
  "older than this page"
check "provenance table lists every source" "$rendered_a" "relationships.json"
check "provenance table lists the review-round dump" "$rendered_a" "dashboard_data.json"

echo "== rebuild with the corpus stamped: the unknown must disappear"
python3 - "$WORK/data" <<'PY'
import json, sys
from datetime import UTC, datetime
from pathlib import Path

path = Path(sys.argv[1]) / "all_issues.json"
raw = json.loads(path.read_text())
# Unwrap first: this stamps the corpus, and stamping something already stamped
# must be a no-op. The old code assumed the file was always a bare list, so
# once the dump began writing {"generated_at", "issues"} it wrapped the
# envelope in a second envelope -- `load_issues` then handed build_qdata a
# dict, iterating it yielded the KEYS, and `.get()` on a string raised. Red on
# v3.0.0 for five days. `load_issues` already accepts either shape; this has
# to as well, or the test pins a shape rather than the property it checks.
issues = raw.get("issues", []) if isinstance(raw, dict) else raw
path.write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "issues": issues}))
PY

python3 "$RETRO/build_dashboard.py" \
  --data-dir "$WORK/data" --template "$RETRO/retro_template.html" --out "$WORK/b.html" \
  | tee "$WORK/b.log"
rendered_b="$(node "$REPO_ROOT/tests/retro_render_check.mjs" "$WORK/b.html")"

refute "no source is unstamped once the corpus carries a stamp" "$(cat "$WORK/b.log")" \
  "unstamped sources: all_issues.json"
refute "the corpus no longer renders as unknown" "$rendered_b" "Issue corpus as of unknown"
check "the corpus renders its own as-of time" "$rendered_b" "Issue corpus as of "
# The stamped object shape must be read as the same corpus, not an empty one.
a_totals="$(grep -o "'issues': [0-9]*" "$WORK/a.log" | head -1)"
b_totals="$(grep -o "'issues': [0-9]*" "$WORK/b.log" | head -1)"
check "both corpus shapes yield the same issue count ($a_totals)" "$b_totals" "$a_totals"

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]]
