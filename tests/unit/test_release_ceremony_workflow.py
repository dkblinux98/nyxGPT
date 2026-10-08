"""The ceremony workflow's checkout must not persist the job token.

`scripts/release_ceremony.sh` pushes master with the owner-level PAT embedded in
the push URL (`https://x-access-token:${PAT}@github.com/...`). `actions/checkout`
defaults to `persist-credentials: true`, which writes the job's own
`GITHUB_TOKEN` into `http.https://github.com/.extraheader` -- and git sends that
header on every github.com request, so it wins over the URL's credential. The
push then authenticates as `github-actions[bot]` with `contents: read` and gets
a 403.

That is not hypothetical: it is how v3.0.0's first real ceremony failed
(run 37432169625, 2026-10-06), at the master fast-forward, after the entry
gate had passed. Nothing earlier exercised it -- v2.1.0 shipped by hand before
the ceremony was automated -- so this test is the only thing that would have.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release_ceremony.yml"


def _checkout_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [s for job in doc["jobs"].values() for s in job.get("steps", [])]
    return [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]


def test_the_ceremony_has_a_checkout_to_check():
    assert (
        _checkout_steps()
    ), "release_ceremony.yml has no actions/checkout step -- update this guard"


def test_checkout_does_not_persist_the_job_token():
    for step in _checkout_steps():
        persisted = (step.get("with") or {}).get("persist-credentials", True)
        assert persisted is False or str(persisted).lower() == "false", (
            "release_ceremony.yml's checkout persists GITHUB_TOKEN; its extraheader "
            "overrides the PAT in release_ceremony.sh's push URL and the master "
            "fast-forward 403s as github-actions[bot]. Set `persist-credentials: false`."
        )


def test_the_ceremony_is_dispatch_only():
    """D-060: no schedule. GitHub throttled the old */15 poll to every 3-8 hours."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc.get(True, doc.get("on"))  # PyYAML reads a bare `on:` key as True
    assert "schedule" not in triggers
    assert "workflow_dispatch" in triggers
