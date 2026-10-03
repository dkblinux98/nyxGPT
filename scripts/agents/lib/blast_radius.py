"""The investigation an agent owes the owner BEFORE escalating (#4134).

Owner decision, 2026-10-03: the retired count-of-2 dispatch pause (#3687) was
standing in for missing investigation. What it protected against was
escalations piling up overnight while nothing got done -- but the defect
underneath is that agents escalate from the tunnel vision of a single issue.
One root cause then surfaces as several separate escalations, and an agent
working an unrelated issue hits the same cause and rediscovers it from
scratch. Counting escalations treated the symptom; looking past the one issue
treats the cause.

So every escalation carries a blast-radius report, and this module is its
shape. Four questions, answered in the escalation comment itself:

  1. is the release branch head red?
  2. are other open issues or PRs failing with the same signature?
  3. has an escalation already gone out for the same cause?
  4. what recent change is the likely common cause?

The report is rendered here, as pure text over gathered facts, so it can be
unit-tested without GitHub and so no call site can quietly ship a shorter
version of it. `blast_radius_report` in scripts/agents/lib/gh_project.sh
gathers the facts; this decides how they read.

**"Not checked" is printed, never omitted.** A question whose fact is absent
(`None`) renders as an explicit "not checked" line. An investigation that
silently skipped a question looks identical to one that asked and found
nothing, and the whole point of this is that the owner can see which of the
two they are reading.

`is_systemic` is the collapse decision: a cause with evidence beyond this one
issue produces ONE escalation for the cause (the later ones link to it rather
than repeating the diagnosis and the Slack DM), which is the same collapse
#3694 applies to cross-issue infrastructure anomalies.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from typing import Any

#: How many recent commits the "likely common cause" line names. Enough to
#: see the suspect, few enough that the report stays readable in a comment.
RECENT_COMMIT_LIMIT = 5

#: The two structured registries the release tracking issue already carries:
#: #3694's cross-issue anomaly markers (`step=<slug> issue=<n>`) and this
#: feature's escalation-cause markers (`cause=<key> origin=<n>`). Question 2 --
#: "is other work failing the same way?" -- is answered from these and from
#: nothing else. A plain text scan of the thread for the signature was the
#: obvious alternative and it is worse than useless: every process marker
#: quotes an issue number ("(#3694)", "(#3730)"), so the answer would be a
#: list of process issues that have nothing to do with the fault.
_REGISTRY_RES = (
    re.compile(r"<!--\s*nyxgpt-anomaly:\s*step=(?P<key>\S+)\s+issue=(?P<ref>\d+)\s+"),
    re.compile(r"<!--\s*escalation-cause:\s*(?P<key>\S+)\s+origin=(?P<ref>\d+)\s*-->"),
)


def same_signature_refs(
    comment_bodies: Iterable[str] | None,
    signature: str | None,
    exclude: Iterable[int] = (),
) -> list[int]:
    """Other issues the release thread already records failing this way.

    Matches the signature case-insensitively against each registry entry's key
    in either direction (a cause key `red-head` matches a step slug
    `review-red-head`, and vice versa), because the two registries slugify
    differently and the question is whether this is the same fault, not
    whether two strings are equal.
    """
    if not signature:
        return []
    needle = signature.casefold()
    skip = {int(n) for n in exclude}
    refs: set[int] = set()
    for body in comment_bodies or []:
        text = body or ""
        for pattern in _REGISTRY_RES:
            for match in pattern.finditer(text):
                key = match.group("key").casefold()
                if needle in key or key in needle:
                    ref = int(match.group("ref"))
                    if ref not in skip:
                        refs.add(ref)
    return sorted(refs)


def _bullet(question: str, answer: str) -> str:
    return f"- **{question}** {answer}"


def is_systemic(findings: dict[str, Any]) -> bool:
    """True when the evidence points past this single issue.

    Any one of: the release branch head is red (every issue building on it is
    affected), another open issue or PR is failing with the same signature, or
    an escalation for this cause is already open. A systemic cause gets one
    escalation; only the affected work is paused -- unrelated work keeps
    dispatching, because nothing about it is broken.
    """
    if findings.get("release_head_red") is True:
        return True
    if findings.get("same_signature"):
        return True
    return findings.get("prior_escalation") is not None


def report(findings: dict[str, Any]) -> str:
    """The markdown block an escalation comment carries.

    `findings` keys, every one optional and every absent one reported as
    unchecked:

      cause              the cause key this escalation is filed under
      signature          the failure signature that was searched for
      release_branch     branch name the head check was run against
      release_head_red   True / False / None (not checked)
      red_checks         names of the failing required checks
      same_signature     ["#123 Review Fix failed on the same step", ...]
      prior_escalation   issue number of an open escalation for this cause
      recent_commits     ["<sha> <subject>", ...], newest first
    """
    lines = ["### Blast radius (investigated before escalating, #4134)", ""]

    cause = findings.get("cause")
    signature = findings.get("signature")
    if cause:
        lines.append(f"- **Cause key:** `{cause}`")
    if signature:
        lines.append(f"- **Signature searched:** `{signature}`")

    branch = findings.get("release_branch") or "the release branch"
    head_red = findings.get("release_head_red")
    if head_red is None:
        lines.append(_bullet(f"Is `{branch}` red?", "not checked."))
    elif head_red:
        checks = findings.get("red_checks") or []
        rendered = ", ".join(f"`{c}`" for c in checks) if checks else "unnamed check(s)"
        lines.append(
            _bullet(
                f"Is `{branch}` red?",
                f"**yes** — {rendered} failing. Everything built on this head is affected.",
            )
        )
    else:
        lines.append(_bullet(f"Is `{branch}` red?", "no."))

    same = findings.get("same_signature")
    if same is None:
        lines.append(_bullet("Other work failing the same way?", "not checked."))
    elif same:
        lines.append(
            _bullet(
                "Other work failing the same way?",
                "**yes** — " + "; ".join(str(s) for s in same) + ".",
            )
        )
    else:
        lines.append(_bullet("Other work failing the same way?", "none found."))

    prior = findings.get("prior_escalation")
    if prior is None:
        lines.append(
            _bullet(
                "Already escalated for this cause?",
                "no open escalation found for this cause.",
            )
        )
    else:
        lines.append(
            _bullet(
                "Already escalated for this cause?",
                f"**yes — #{prior}.** That escalation is the one to answer; "
                "this issue is linked to it rather than diagnosed again.",
            )
        )

    commits = findings.get("recent_commits")
    if commits is None:
        lines.append(_bullet("Likely common cause?", "recent changes not checked."))
    elif commits:
        rendered = "; ".join(str(c) for c in commits[:RECENT_COMMIT_LIMIT])
        lines.append(
            _bullet(
                "Likely common cause?",
                f"most recent changes on `{branch}`: {rendered}.",
            )
        )
    else:
        lines.append(_bullet("Likely common cause?", f"no recent changes on `{branch}`."))

    lines.append("")
    lines.append(
        "_Systemic: one escalation per cause, not one per affected issue. "
        "Unrelated work keeps dispatching._"
        if is_systemic(findings)
        else "_No evidence beyond this issue — escalated as a one-off._"
    )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"report", "systemic", "same-signature"}:
        print(
            "usage: blast_radius.py {report|systemic|same-signature <signature> [exclude]}"
            "  # JSON on stdin",
            file=sys.stderr,
        )
        return 2
    if argv[0] == "same-signature":
        if len(argv) < 2:
            print("usage: same-signature <signature> [exclude_issue]", file=sys.stderr)
            return 2
        exclude = [int(argv[2])] if len(argv) > 2 and argv[2].isdigit() else []
        # stdin: JSON array of the release issue's comment bodies.
        print(json.dumps(same_signature_refs(json.load(sys.stdin), argv[1], exclude)))
        return 0
    findings = json.load(sys.stdin)
    if argv[0] == "report":
        print(report(findings))
    else:
        print("true" if is_systemic(findings) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
