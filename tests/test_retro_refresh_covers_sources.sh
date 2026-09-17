#!/usr/bin/env bash
# The all-in-one refresh workflow produces every input the dashboard reads.
#
# Which question does this answer?
#
#   "Can a refresh pass that dispatches retro_data_refresh.yml once still
#    leave an input for the session to generate by hand -- or a dump it has
#    to remember to dispatch separately?"
#
# The defect (2026-08-17 → 09-17): the refresh session chose which dumps to
# dispatch and generated the rest locally, so each pass refreshed a different
# subset and project_fields.json -- which nothing but a workflow can produce --
# went five days stale while every pass reported success. The fix is that one
# workflow owns every input. This pins it: the set of files build_dashboard.py
# tracks as sources, and the set of dump scripts the single-file dumps run,
# must both be covered by retro_data_refresh.yml. Adding a tracked source or a
# dump without wiring it into the refresh fails here, before it can quietly
# become the next hand-written file.
#
# Usage: bash tests/test_retro_refresh_covers_sources.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REFRESH="$REPO_ROOT/.github/workflows/retro_data_refresh.yml"
BUILDER="$REPO_ROOT/scripts/retrospective/build_dashboard.py"

pass=0
fail=0
ok()   { echo "  ok: $1"; pass=$((pass + 1)); }
bad()  { echo "  FAIL: $1" >&2; fail=$((fail + 1)); }

[[ -f "$REFRESH" ]] || { echo "missing $REFRESH" >&2; exit 1; }

echo "== every tracked source file is published by the refresh workflow"
# The tracked sources are the (key, label, filename, ...) tuples handed to
# source_stamps() in main(); read them from the builder itself so this test
# cannot drift from the list it checks.
mapfile -t tracked < <(python3 - "$BUILDER" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
main = text[text.index("def main("):]
seen = []
for m in re.finditer(r'\(\s*"(\w+)",\s*"[^"]+",\s*"([^"]+\.json)"', main):
    if m.group(2) not in seen:
        seen.append(m.group(2))
print("\n".join(seen))
PY
)
[[ ${#tracked[@]} -ge 7 ]] && ok "builder tracks ${#tracked[@]} source files" \
  || bad "expected at least 7 tracked sources in build_dashboard.py, found ${#tracked[@]}"
for f in "${tracked[@]}"; do
  if grep -q "\"\$d/$f\"" "$REFRESH"; then
    ok "$f is published by retro_data_refresh.yml"
  else
    bad "$f is a tracked dashboard source but retro_data_refresh.yml never publishes it"
  fi
done

echo "== every single-file dump's script is run by the refresh workflow"
for wf in "$REPO_ROOT"/.github/workflows/retro_*_dump.yml; do
  mapfile -t scripts < <(grep -v '^[[:space:]]*#' "$wf" \
    | grep -o 'scripts/retrospective/dump_[a-z_]*\.\(py\|sh\)' | sort -u)
  [[ ${#scripts[@]} -gt 0 ]] || bad "$(basename "$wf") runs no scripts/retrospective/dump_* script"
  for s in "${scripts[@]}"; do
    if grep -v '^[[:space:]]*#' "$REFRESH" | grep -q "$s"; then
      ok "$(basename "$wf") → $s is also run by retro_data_refresh.yml"
    else
      bad "$(basename "$wf") runs $s but retro_data_refresh.yml does not"
    fi
  done
done

echo "== the refresh reports a dump that did not land instead of skipping it"
grep -q 'continue-on-error: true' "$REFRESH" \
  && ok "dumps continue past one failure" || bad "dumps do not continue past a failure"
grep -q 'Report every dump that did not land' "$REFRESH" \
  && ok "the run names what did not land" || bad "no reporting step"
grep -q 'await_rate_limit.sh' "$REFRESH" \
  && ok "each dump waits for the REST budget" || bad "no rate-limit wait"

echo "== the single-file re-run path waits for the REST budget too"
# The re-run of one failed dump is the moment the budget is most likely spent
# (the dump may have failed because of it), so the fix lives in every dump
# workflow, not only in the refresh.
for wf in "$REPO_ROOT"/.github/workflows/retro_*_dump.yml; do
  if grep -v '^[[:space:]]*#' "$wf" | grep -q 'await_rate_limit.sh'; then
    ok "$(basename "$wf") waits for the REST budget before its dump"
  else
    bad "$(basename "$wf") dumps without waiting for the REST budget"
  fi
done

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]]
