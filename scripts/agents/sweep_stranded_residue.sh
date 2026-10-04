#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  sweep_stranded_residue.sh [--open-prs] [base_branch]

The one-off, repo-wide answer to "is any branch carrying work that was pushed
after its own pull request merged?" (#4151, acceptance criterion 5).

WHY A SWEEP AS WELL AS THE PER-RUN GUARD. developer_ensure_pr_exists.sh now
detects this at the end of the run that causes it, which covers every FUTURE
occurrence. It does nothing about the ones already on the remote -- and there
was at least one when this was written: `fix/3986-*` held 579 insertions
(a CI job, a 214-line test module, src/nyxgpt/ops.py, a dashboard page) pushed
19 minutes after PR #4126 merged, implementing the one acceptance criterion the
owner could not test, while #3986 sat closed in `For Release` reading as
accepted. Finding existing strandings needs a pass over the whole remote once;
finding future ones does not.

WHAT IT REPORTS. Every `claude/*`, `feat/*`, `fix/*` and `chore/*` branch on
origin whose `branch_pr_disposition` (lib/gh_project.sh ->
lib/branch_residue.py) is `merged-residue`: a pull request of that head merged
into base_branch, and the branch has since received commits whose content is
NOT on base_branch in any form. Same candidate set as
reconcile_dead_branches.sh and the same classifier the per-run guard uses, so
the three cannot disagree about one branch.

REPORT ONLY unless --open-prs is passed, in which case each stranded branch
gets the same draft residue PR and loud issue comment the per-run guard opens.
Reporting is the default for the reason reconcile_dead_branches.sh reports by
default: a sweep that acts on every branch in the repo on its first run is the
one whose blast radius nobody checked.

NOTHING SCHEDULES THIS, and nothing should (ledger D-013, first principle 1):
branch hygiene is event-driven and the event now has a guard on it. This is the
human/agent question "what is still out there?", available as a
`workflow_dispatch` run of .github/workflows/stranded_residue_sweep.yml so it
can be answered without a terminal.

Exit status is 0 whether or not anything is found -- the report is the output.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then
  load_config; require_gh_auth; require_cmd git; require_cmd jq
  echo "OK"; exit 0
fi

# Report-only default, as above. `DRY_RUN` is the variable open_residue_pr
# reads, and the two callers of that function agree on its meaning.
DRY_RUN=1
case "${1:-}" in
  --open-prs) DRY_RUN=0; shift ;;
  --report) shift ;;   # the default; accepted so a caller can be explicit
esac
export DRY_RUN

load_config
require_gh_auth
require_cmd git
require_cmd jq

BASE_BRANCH="${1:-$(get_release_branch)}"

echo "[residue-sweep] base_branch=${BASE_BRANCH} report_only=${DRY_RUN}" >&2
git fetch origin "$BASE_BRANCH" >&2 || true

mapfile -t CANDIDATES < <(git ls-remote --heads origin 2>/dev/null \
  | awk '{print $2}' | sed 's#^refs/heads/##' \
  | grep -E '^(claude|feat|fix|chore)/' || true)

echo "[residue-sweep] ${#CANDIDATES[@]} candidate branch(es) on origin." >&2

stranded=0
checked=0
unknown=0

for branch in "${CANDIDATES[@]}"; do
  [[ -n "$branch" ]] || continue
  [[ "$branch" != "$BASE_BRANCH" && "$branch" != "master" && "$branch" != "main" ]] || continue
  checked=$((checked + 1))

  disposition_json="$(branch_pr_disposition "$branch" "$BASE_BRANCH" 2>/dev/null || echo '{}')"
  disposition="$(jq -r '.disposition // "unknown"' <<<"$disposition_json" 2>/dev/null || echo unknown)"
  merged_prs="$(jq -r '(.merged_prs // []) | map("#" + tostring) | join(", ")' <<<"$disposition_json" 2>/dev/null || echo "")"

  case "$disposition" in
    merged-residue)
      stranded=$((stranded + 1))
      mapfile -t shas < <(branch_unmerged_shas "$branch" "$BASE_BRANCH")
      issue="$(extract_issue_number "$branch")"
      echo "[residue-sweep] ::warning::STRANDED ${branch} — ${#shas[@]} commit(s) pushed after ${merged_prs:-its PR} merged, not on ${BASE_BRANCH}${issue:+ (issue #${issue})}" >&2
      for sha in "${shas[@]}"; do
        [[ -n "$sha" ]] || continue
        echo "[residue-sweep]     ${sha} $(git log -1 --format='%s' "$sha" 2>/dev/null || echo '(subject unavailable)')" >&2
      done
      if [[ "$DRY_RUN" == "1" ]]; then
        echo "[residue-sweep]     report only — re-run with --open-prs to open a draft PR for it" >&2
      else
        open_residue_pr "$branch" "$BASE_BRANCH" "$issue" "$merged_prs" "${shas[@]}" >/dev/null \
          || _warn "Could not open a residue PR for ${branch}."
      fi
      ;;
    unknown)
      unknown=$((unknown + 1))
      echo "[residue-sweep] ?     ${branch} — could not be classified ($(jq -r '.reason // "see above"' <<<"$disposition_json" 2>/dev/null || echo "see above"))" >&2
      ;;
    *)
      echo "[residue-sweep] ok    ${branch} — ${disposition}" >&2
      ;;
  esac
done

# Named counts, including the unclassifiable ones: a sweep that silently
# skipped branches reads as "nothing out there" when it means "I could not
# tell", and that misreading is the whole defect this script exists for.
echo "[residue-sweep] Done. checked=${checked} stranded=${stranded} unclassified=${unknown} report_only=${DRY_RUN}" >&2
if [[ "$stranded" -gt 0 ]]; then
  echo "[residue-sweep] ${stranded} branch(es) carry work that is on ${BASE_BRANCH} in no form. See the STRANDED lines above." >&2
fi
