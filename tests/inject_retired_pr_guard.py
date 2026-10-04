#!/usr/bin/env python3
"""Restore the retired "does a PR exist?" guard, for fault injection (#4151).

    python3 tests/inject_retired_pr_guard.py <ensure_pr_script> <output_path>

`developer_ensure_pr_exists.sh` used to decide what to do about a branch by
counting its pull requests: any PR at all -- open, closed or merged -- meant
the work was routed, so the script stopped. That is the #4151 defect. A branch
whose PR is MERGED and which then receives commits answers "yes, it has a pull
request" while carrying work that is on the release branch in no form.

Two shell suites inject that retired form, by cutting the sentinel-delimited
disposition block out of the real script and putting the old counter back, so
what runs is the actual retired code path rather than a re-description of it:

  * `tests/test_stranded_residue.sh` runs it against a merged-PR-plus-residue
    fixture and shows it opening nothing -- without which the new assertions
    would also pass against an implementation that happens to be right by luck
    (#3775: a check that cannot fail proves nothing).
  * `tests/test_ensure_pr_exists.sh` runs it against an unreadable PR list and
    shows the one-pipeline form dying on `set -u` two lines later, because `jq`
    prints `0` on empty stdin and `pipefail` then appends the fallback's own
    line: `pr_count` holds $'0\\nunknown', which is neither `== unknown` nor
    `-gt 0`. The guard that promised to fail closed failed open.

One helper for both, because the injected text IS the retired implementation:
two copies of it would be two different claims about what used to be there.
"""

from __future__ import annotations

import re
import sys

# The form that shipped, verbatim in shape: the one-line count with its dead
# `unknown` check, followed by the any-PR short circuit.
RETIRED_GUARD = """pr_count="$(gh api "repos/${REPO}/pulls?head=${REPO_OWNER}:${BRANCH}&state=all&per_page=100" \\
    --paginate 2>/dev/null | jq -s '[.[][]] | length' || echo "unknown")"
if [[ "$pr_count" == "unknown" ]]; then
  _warn "Could not list PRs for ${BRANCH}; leaving it alone rather than opening a duplicate."
  exit 0
fi
if [[ "$pr_count" -gt 0 ]]; then
  echo "[ensure-pr] ${BRANCH} already has ${pr_count} pull request(s); nothing to do." >&2
  exit 0
fi
"""

_BLOCK_RE = re.compile(
    r"# >>> disposition \(#4151\) >>>\n.*?# <<< disposition \(#4151\) <<<\n",
    re.S,
)


def inject(text: str) -> str:
    """Replace the disposition block in `text` with the retired guard."""
    block = _BLOCK_RE.search(text)
    assert block, "could not locate the sentinel-delimited disposition block"
    return text[: block.start()] + RETIRED_GUARD + text[block.end() :]


def main(argv: list[str]) -> int:
    """CLI used by the shell suites."""
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src, out = argv
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(inject(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
