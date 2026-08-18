"""The `Support` label and what it means to the agent loop (#3745).

A support report is filed by a *user* through the web UI's Support -> File an
Issue link, which opens `.github/ISSUE_TEMPLATE/support.yml`; the template
declares `labels: [Support]`, so the label lands on every such report no
matter who files it.

That holds only while the label exists. GitHub drops a template-declared
label that does not exist, silently, and the form keeps accepting tickets
that carry nothing -- which makes every guard below inert at once, because
they all test the same absent name. That is #3810: filed unlabeled, assigned
to the scrummaster seven seconds later, caught by a human five minutes after
that. The label is therefore guaranteed rather than assumed --
`admin_ensure_support_label.yml` re-asserts and verifies it on a schedule,
and `support_intake_guard.yml` fails loudly on any ticket that slips through
without it (#3811).

That label is a boundary, not a category. A `Support`-labeled issue:

* is never added to the code project, stamped with project fields, put in a
  sprint, or selected for implementation -- the issue-hygiene and assignment
  workflows skip it outright, and the backlog summarizer refuses it as a
  candidate even if one somehow reached the board;
* is routed instead onto the separate **nyxGPT Support** project by that
  project's own auto-add workflow (filter `is:issue is:open label:Support`),
  which is owner-configured and which agent code must never add to, read
  from, or depend on. The ticket's TYPE -- Bug Found / Feature Request /
  Question -- is a field on that project, not a label: the intake collects it
  into the issue body and the owner sets the field at triage (#3811).

Adopting a support report into engineering is the owner's act: they file (or
convert it to) a normal issue on the code project. Nothing here does that
automatically.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SUPPORT_LABEL = "Support"


def label_names(labels: Iterable[Any] | None) -> list[str]:
    """Extract label names from either payload shape GitHub hands us.

    The REST/webhook issue payload carries `labels` as objects
    (`[{"name": "Support"}]`); the GraphQL project query and several `gh
    --jq` call sites flatten it to plain strings. Callers should not have to
    care which one they got.
    """
    names = []
    for label in labels or []:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def is_support_issue(labels: Iterable[Any] | None) -> bool:
    """True when these labels mark a user support report the agent loop skips."""
    return SUPPORT_LABEL in label_names(labels)
