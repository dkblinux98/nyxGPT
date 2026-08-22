#!/usr/bin/env bash
# Pins the three classification rules in scripts/agents/round_state.sh.
#
# Each rule exists because a human reader was misled by its absence on
# 2026-08-22 (see the script's header). The stubs below make the three
# situations reproducible without a network: a reopened issue whose old fix
# merged, the same but with an open blocker, and an issue with nothing at all.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1"; fails=$((fails + 1)); }

stub="$(mktemp -d)"
trap 'rm -rf "$stub"' EXIT

cat > "$stub/git" <<'G'
#!/usr/bin/env bash
case "$*" in
  *"ls-remote"*) exit 0 ;;                    # no branches anywhere
  *"rev-list"*)  echo 0 ;;
  *)             exit 0 ;;
esac
G

cat > "$stub/gh" <<'H'
#!/usr/bin/env bash
args="$*"
# issue state
if [[ "$args" == *"issue view 100"* || "$args" == *"issue view 200"* || "$args" == *"issue view 300"* ]]; then
  echo OPEN; exit 0
fi
# blocked_by: only 200 has an open blocker
if [[ "$args" == *"issues/200/dependencies/blocked_by"* ]]; then echo "999"; exit 0; fi
if [[ "$args" == *"dependencies/blocked_by"* ]]; then echo ""; exit 0; fi
# PR list: 100 and 200 each have a MERGED closing PR; 300 has none
if [[ "$args" == *"pr list"* ]]; then
  cat <<'J'
[{"number":11,"state":"MERGED","mergedAt":"2026-08-01T00:00:00Z","reviewDecision":null,"mergeable":"MERGEABLE","closingIssuesReferences":[{"number":100}]},
 {"number":22,"state":"MERGED","mergedAt":"2026-08-01T00:00:00Z","reviewDecision":null,"mergeable":"MERGEABLE","closingIssuesReferences":[{"number":200}]}]
J
  exit 0
fi
exit 0
H
chmod +x "$stub/git" "$stub/gh"

out="$(PATH="$stub:$PATH" ROUND_STATE_NO_CONFIG=1 REPO_OWNER=o REPO_NAME=r PROJECT_NUMBER=1 PROJECT_OWNER=o \
      RELEASE_BRANCH=v3.0.0 bash "$ROOT/scripts/agents/round_state.sh" 100 200 300 2>/dev/null)"

grep -q "REOPENED after PR#11" <<<"$out" \
  && pass "an OPEN issue whose newest PR merged is reported as reopened, not merged" \
  || fail "reopened rule: got: $(grep '#100' <<<"$out")"

grep -q "WAITING on blockers" <<<"$out" \
  && pass "a reopened issue with an open blocker is WAITING, not needing work" \
  || fail "blocker rule: got: $(grep '#200' <<<"$out")"

grep -q "NOTHING" <<<"$out" \
  && pass "an issue with no PR and no branch is reported as not started" \
  || fail "not-started rule: got: $(grep '#300' <<<"$out")"

grep -q "MERGED PR#11" <<<"$out" \
  && fail "regression: a reopened issue was reported as MERGED" \
  || pass "no reopened issue is ever labelled MERGED"

echo
[[ "$fails" -eq 0 ]] && { echo "round_state: all checks passed"; exit 0; }
echo "round_state: $fails check(s) failed"; exit 1
