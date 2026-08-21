"""The required-check set stays honest about the workflows it names (#3971).

`.github/required-checks.txt` is the named list that decides whether a PR head
is reviewable: a required check that failed refuses the submission, a required
check still running makes the review trigger wait, and anything not named is
neither. That design has exactly one failure mode, and it is silent both ways:

* a name that matches no job any more (a renamed job, a deleted workflow) is a
  gate that can never fire -- the list still *looks* like it covers that
  ground, and nothing goes red to say otherwise;
* a new `pull_request` job that nobody added to the list is a real gate the
  review never waits for, so the reviewer sees a green-looking head while the
  job is still running or already red.

So the invariant is stronger than "the names resolve": every job of every
`pull_request`-triggered workflow must be classified as required or explicitly
not-required. Adding a smoke workflow therefore forces the decision instead of
defaulting to "not a gate", which is the direction that fails quietly.

The `not-required` section is not an escape hatch to be widened casually --
it is the record of what the project decided cannot gate a review (the
review's own check runs, which would deadlock the gate against itself, and
project bookkeeping, which judges no code).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_CHECKS = REPO_ROOT / ".github" / "required-checks.txt"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _parse_sections(text: str) -> dict[str, list[str]]:
    """Same parse `required_check_names` does in gh_project.sh."""
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("["):
            current = stripped.strip("[]").strip()
            sections.setdefault(current, [])
            continue
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _check_names(pull_request_only: bool) -> dict[str, str]:
    """Check-run name -> workflow filename.

    The check-run name is the job's ``name:`` when it has one and its job id
    otherwise -- which is why the list carries sentences like "Install the
    working tree's formulas" beside ids like ``k8s-artifact-smoke``.

    ``pull_request_only`` separates the two questions this file has to answer.
    *Completeness* is about jobs that run on a PR head, because those are the
    ones a review can be racing. *Resolvability* is about every job in the
    repository: a check run can reach a PR head from a workflow with no
    `pull_request` trigger at all -- `execute-review-decision` arrives by
    `issue_comment`, and ci-tests' jobs arrive by `push` and report as
    skipped -- and classifying those is legitimate, not a dangling name.
    """
    names: dict[str, str] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        # PyYAML resolves the bare `on:` key to the boolean True.
        triggers = data.get("on", data.get(True))
        if not isinstance(triggers, dict):
            continue
        if pull_request_only and "pull_request" not in triggers:
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            job = job or {}
            names[job.get("name") or job_id] = path.name
    return names


def _pull_request_check_names() -> dict[str, str]:
    return _check_names(pull_request_only=True)


def test_every_pull_request_job_is_classified() -> None:
    sections = _parse_sections(REQUIRED_CHECKS.read_text())
    classified = set(sections.get("required", [])) | set(sections.get("not-required", []))

    unclassified = {
        name: wf for name, wf in _pull_request_check_names().items() if name not in classified
    }

    assert not unclassified, (
        "These jobs run on pull requests but .github/required-checks.txt never says "
        "whether they gate a review, so the review would start (or approve) while they "
        "are still running or already red: "
        + ", ".join(f"{name} ({wf})" for name, wf in sorted(unclassified.items()))
    )


def test_every_named_check_resolves_to_a_real_job() -> None:
    sections = _parse_sections(REQUIRED_CHECKS.read_text())
    known = set(_check_names(pull_request_only=False))

    dangling = [
        name
        for name in sections.get("required", []) + sections.get("not-required", [])
        if name not in known
    ]

    assert not dangling, (
        "These names in .github/required-checks.txt match no job of any "
        "workflow job in this repository, so they are gates that can never fire: "
        + ", ".join(sorted(dangling))
    )


def test_the_two_sections_do_not_overlap() -> None:
    sections = _parse_sections(REQUIRED_CHECKS.read_text())
    overlap = set(sections.get("required", [])) & set(sections.get("not-required", []))
    assert not overlap, (
        "A check cannot be both required and not-required; the gate reads the "
        "required section, so the not-required entry would be the copy nobody runs: "
        + ", ".join(sorted(overlap))
    )


def test_the_review_own_checks_are_never_required() -> None:
    """The one entry that must never move sections.

    `claude-review` is the check run of the very job the gate decides whether
    to start. Requiring it would make the gate wait for its own result, which
    is the deadlock Q-005 records for the merge path -- and unlike a wrong
    name, this one would jam every review in the project rather than let one
    through.
    """
    sections = _parse_sections(REQUIRED_CHECKS.read_text())
    required = set(sections.get("required", []))
    for own in ("claude-review", "execute-review-decision"):
        assert own not in required, (
            f"{own} is the review's own check run; requiring it deadlocks the "
            "review gate against itself."
        )
