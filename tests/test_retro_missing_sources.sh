#!/usr/bin/env bash
# Executed proof that a retrospective dump which did not land is visible (#3808).
#
# Which question does this answer?
#
#   "When a dump fails and its data file never appears, does the dashboard SAY
#    the section is unavailable and which dump owes it — and does the build
#    refuse to report success?"
#
# This is the defect itself, not a hypothetical: retro_churn_dump.yml failed on
# every run it ever had, build_dashboard.py dropped the churn section, and the
# refreshed dashboard looked complete. Nothing anywhere reported it. So the
# claim under test is about what a reader sees and what the build exits with,
# which only running both can show.
#
# Both halves are injected, per the fault-injection rule (#3753): the same
# assertions are re-run with the files restored, where the notices must be
# absent and the build must exit 0. A one-sided lab passes with the fix removed.
#
# Usage: bash tests/test_retro_missing_sources.sh
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
equals() { # equals <description> <actual> <expected>
  if [[ "$2" == "$3" ]]; then
    echo "  ok: $1"
    pass=$((pass + 1))
  else
    echo "  FAIL: $1"
    echo "        expected: $3"
    echo "        actual:   $2"
    fail=$((fail + 1))
  fi
}
field() { # field <json> <python expression over `d`>
  printf '%s' "$1" | python3 -c "import json,sys; d=json.load(sys.stdin); print($2)"
}

cp -R "$RETRO/data" "$WORK/data"

echo "== the failing case: neither dump has ever landed"
rm -f "$WORK/data/spend.json" "$WORK/data/churn.json"
status=0
python3 "$RETRO/build_dashboard.py" \
  --data-dir "$WORK/data" --template "$RETRO/retro_template.html" --out "$WORK/missing.html" \
  >"$WORK/missing.log" 2>&1 || status=$?
cat "$WORK/missing.log"
missing_log="$(cat "$WORK/missing.log")"

equals "the build refuses to report success" "$status" "2"
check "the build names the missing files" "$missing_log" \
  "missing sources: spend.json (retro_spend_dump.yml), churn.json (retro_churn_dump.yml)"
check "the build says which dump owes each one" "$missing_log" \
  "spend.json was not produced by retro_spend_dump.yml"
check "the page is still written, so publishing stays possible deliberately" \
  "$(test -s "$WORK/missing.html" && echo written)" "written"

rendered="$(node "$REPO_ROOT/tests/retro_render_check.mjs" "$WORK/missing.html")"
spend_notice="$(field "$rendered" "d['unavailable']['spendunavail']['html']")"
churn_notice="$(field "$rendered" "d['unavailable']['churnunavail']['html']")"

equals "the spend notice is shown" \
  "$(field "$rendered" "d['unavailable']['spendunavail']['hidden']")" "False"
equals "the churn notice is shown" \
  "$(field "$rendered" "d['unavailable']['churnunavail']['hidden']")" "False"
equals "the spend data body stays hidden" \
  "$(field "$rendered" "d['bodies']['spendbody']['hidden']")" "True"
equals "the churn data body stays hidden" \
  "$(field "$rendered" "d['bodies']['churnbody']['hidden']")" "True"
check "the notice says unavailable, not zero" "$spend_notice" "Section unavailable"
check "the notice names the missing file" "$spend_notice" "data/spend.json"
check "the notice names the dump that owes it" "$spend_notice" "retro_spend_dump.yml"
check "the notice links that workflow's runs" "$spend_notice" \
  "actions/workflows/retro_spend_dump.yml"
check "the notice carries the build time, so a reader knows when it was missing" \
  "$spend_notice" "was not present when this page was built"
check "the churn notice names its own dump" "$churn_notice" "retro_churn_dump.yml"
check "the header counts the unavailable sections" "$(field "$rendered" "d['buildstamp']")" \
  "2 sections unavailable"
check "the provenance table marks the file absent" \
  "$(field "$rendered" "d['provenancerows']")" "file absent — section unavailable"

echo "== the same assertions with both dumps landed: nothing may be flagged"
cp "$RETRO/data/spend.json" "$RETRO/data/churn.json" "$WORK/data/"
status=0
python3 "$RETRO/build_dashboard.py" \
  --data-dir "$WORK/data" --template "$RETRO/retro_template.html" --out "$WORK/present.html" \
  >"$WORK/present.log" 2>&1 || status=$?
cat "$WORK/present.log"

equals "a complete build exits 0" "$status" "0"
check "the build reports nothing missing" "$(cat "$WORK/present.log")" "missing sources: none"

rendered_ok="$(node "$REPO_ROOT/tests/retro_render_check.mjs" "$WORK/present.html")"
equals "the spend notice is hidden when the dump landed" \
  "$(field "$rendered_ok" "d['unavailable']['spendunavail']['hidden']")" "True"
equals "the churn notice is hidden when the dump landed" \
  "$(field "$rendered_ok" "d['unavailable']['churnunavail']['hidden']")" "True"
equals "the spend data body is shown" \
  "$(field "$rendered_ok" "d['bodies']['spendbody']['hidden']")" "False"
equals "the churn data body is shown" \
  "$(field "$rendered_ok" "d['bodies']['churnbody']['hidden']")" "False"
refute "the header claims no unavailable sections" \
  "$(field "$rendered_ok" "d['buildstamp']")" "sections unavailable"
refute "the provenance table marks nothing absent" \
  "$(field "$rendered_ok" "d['provenancerows']")" "file absent"

echo "== the deliberate override still builds green"
rm -f "$WORK/data/churn.json"
status=0
python3 "$RETRO/build_dashboard.py" --allow-missing-sources \
  --data-dir "$WORK/data" --template "$RETRO/retro_template.html" --out "$WORK/override.html" \
  >"$WORK/override.log" 2>&1 || status=$?
equals "--allow-missing-sources exits 0" "$status" "0"
check "and still names what is missing" "$(cat "$WORK/override.log")" \
  "missing sources: churn.json (retro_churn_dump.yml)"
override="$(node "$REPO_ROOT/tests/retro_render_check.mjs" "$WORK/override.html")"
equals "the page still tells the reader the section is unavailable" \
  "$(field "$override" "d['unavailable']['churnunavail']['hidden']")" "False"

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]]
