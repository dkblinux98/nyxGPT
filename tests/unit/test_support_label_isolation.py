"""`Support`-labeled issues are invisible to the agent loop (#3745).

The owner decision behind the Support menu is that a user support report is
routed onto a separate project (nyxGPT Support, project 5) that agents have
no access to, and that the agent-side automation skips such issues entirely:
no add to the code project, no field stamping, no sprint, no selection.

That is asserted here rather than left to convention -- a workflow guard
deleted in a later edit, or a selector that starts treating Support issues as
backlog, would let a user's support report be picked up and "implemented" by
the developer agent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_LIB = _REPO_ROOT / "scripts" / "agents" / "lib"

sys.path.insert(0, str(_LIB))

from support_label import SUPPORT_LABEL, is_support_issue, label_names  # noqa: E402


def _job_conditions(workflow: str) -> dict[str, str]:
    spec = yaml.safe_load((_WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return {name: str(job.get("if", "")) for name, job in spec["jobs"].items()}


def test_the_issue_form_template_applies_the_support_label():
    """Template-declared labels apply for every filer, including non-collaborators."""
    template = yaml.safe_load(
        (_REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "support.yml").read_text(encoding="utf-8")
    )
    assert template["labels"] == [SUPPORT_LABEL]

    # `Support` is the ONLY label the form applies: it is the routing key the
    # Support project's auto-add matches and the agent loop skips on. The
    # ticket TYPE is not a label (#3811) -- see the next test.
    field_ids = {field.get("id") for field in template["body"] if field.get("id")}
    assert field_ids == {"ticket_type", "what_happened", "version", "platform"}


def test_the_form_collects_the_ticket_type_as_a_field():
    """Every ticket must arrive classified, and only a form field can do it.

    The Support project types tickets with a `Ticket Type` project FIELD, and
    GitHub offers no mechanism that maps a form answer onto either a label or
    a project field: `labels:` is a static template-level list and a dropdown
    answer lands in the issue body. So the type is asked as a field, rendered
    into the body, and set on the project at triage (#3811). Before this,
    nothing asked at all and every ticket arrived needing the owner to infer
    it.
    """
    template = yaml.safe_load(
        (_REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "support.yml").read_text(encoding="utf-8")
    )
    (field,) = [f for f in template["body"] if f.get("id") == "ticket_type"]
    assert field["type"] == "dropdown"
    assert field["validations"]["required"] is True

    # The options are the product's own list, so a link built by
    # `nyxgpt.support` prefills a value the form actually offers -- GitHub
    # ignores a prefill that matches no option, which would silently demote
    # this to "unanswered required field".
    from nyxgpt.support import TICKET_TYPES

    assert tuple(field["attributes"]["options"]) == TICKET_TYPES

    # A type is never a label: adding one here would put a second label on
    # every ticket and split the routing key.
    assert template["labels"] == [SUPPORT_LABEL]


def test_the_support_label_creation_path_actually_runs():
    """A creation path nobody ever invokes is not a guarantee (#3811).

    This workflow existed from #3745 and was dispatch-only. It had ZERO runs
    in its entire history, so the label did not exist, so the template
    applied nothing -- silently -- and #3810 was filed unlabeled and assigned
    to the scrummaster seven seconds later. Being dispatchable is therefore
    not what this test asserts: being *invoked without anyone remembering to*
    is.
    """
    body = (_WORKFLOWS / "admin_ensure_support_label.yml").read_text(encoding="utf-8")
    spec = yaml.safe_load(body)
    # `on:` parses as the YAML boolean True -- 1.1 semantics.
    triggers = spec.get("on") or spec.get(True)

    assert "workflow_dispatch" in triggers, "the owner must still be able to run it on demand"
    assert (
        "schedule" in triggers
    ), "the label can be deleted at any time; re-assert it on a schedule"
    assert "push" in triggers, "and immediately when the form that declares the label changes"
    assert ".github/ISSUE_TEMPLATE/support.yml" in triggers["push"]["paths"]

    assert "gh label create" in body
    assert "--force" in body, "label creation must be idempotent"
    assert SUPPORT_LABEL in body


def test_a_missing_support_label_fails_the_run_rather_than_degrading():
    """The check that fails if the label is absent from the repository (#3811).

    `gh label create` exiting 0 is not evidence the label is there, and the
    whole defect class here is silence: a label that does not exist produces
    no error anywhere, just tickets that route nowhere. So creation is
    followed by a verification that reads the label list back and exits
    non-zero when the name is absent.
    """
    body = (_WORKFLOWS / "admin_ensure_support_label.yml").read_text(encoding="utf-8")
    spec = yaml.safe_load(body)
    steps = spec["jobs"]["ensure-label"]["steps"]
    verify = [s for s in steps if "verify" in s.get("name", "").lower()]
    assert verify, "creation must be followed by a verification step"

    run = verify[0]["run"]
    assert "gh label list" in run
    # Exact-match, not `--search`: `gh label list --search` is fuzzy, and a
    # substring hit on some other label would be exactly the false assurance
    # that let #3810 through.
    assert "grep -Fxq" in run
    assert "--search" not in run
    assert "exit 1" in run
    assert "::error::" in run


def test_an_unlabeled_support_ticket_is_repaired_and_goes_red():
    """The backstop for a ticket filed while the label is missing (#3811).

    Every guard around a support ticket keys on the label, so an unlabeled
    one is invisible to all of them at once -- which is why #3810 needed a
    human to notice at 04:33. This workflow fires on exactly that shape,
    repairs the ticket, and then fails ON PURPOSE: the ticket is fixable
    automatically, the silence is not.
    """
    path = _WORKFLOWS / "support_intake_guard.yml"
    body = path.read_text(encoding="utf-8")
    spec = yaml.safe_load(body)

    assert "issues" in (spec.get("on") or spec.get(True))

    condition = _job_conditions("support_intake_guard.yml")["repair-and-alert"]
    # Fires only on a support-shaped issue that arrived WITHOUT the label --
    # the degraded case by construction. A correctly-labeled ticket, and any
    # agent-filed issue, start no runner at all.
    assert f"!contains(github.event.issue.labels.*.name, '{SUPPORT_LABEL}')" in condition
    assert "startsWith(github.event.issue.title, 'support:')" in condition
    assert "### Installed version" in condition

    steps = spec["jobs"]["repair-and-alert"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)
    # Repair: the label back on, and the agent loop off.
    assert '--add-label "$LABEL"' in runs
    assert "--remove-assignee" in runs
    # And the alert. A green run here would restore the silence.
    assert steps[-1]["run"].rstrip().endswith("exit 1")
    assert "::error::" in steps[-1]["run"]


def test_the_live_label_check_reads_and_never_writes():
    """The label's existence is checked against the real repository (#3811).

    A unit test cannot see the repository's labels, and that is exactly where
    #3810's cause lived: every file in the tree was correct and the label was
    simply not there. So the check runs in CI against the live repo -- and it
    must stay read-only, because a check that creates what it is checking for
    always passes.
    """
    spec = yaml.safe_load((_WORKFLOWS / "support-intake-smoke.yml").read_text(encoding="utf-8"))
    job = spec["jobs"]["label-exists"]
    assert job["env"]["LABEL"] == SUPPORT_LABEL

    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "gh label list" in runs
    assert "gh label create" not in runs, "the check must not create what it checks for"
    assert "gh issue" not in runs, "the smoke job files nothing"
    assert spec["permissions"] == {"contents": "read"}


def test_blank_issues_stay_enabled_for_the_agent_loop():
    """Agent-filed issues follow CLAUDE.md's body structure, not the support form."""
    config = yaml.safe_load(
        (_REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    )
    assert config["blank_issues_enabled"] is True


def test_label_helpers_read_both_payload_shapes():
    # Webhook/REST shape.
    assert is_support_issue([{"name": SUPPORT_LABEL}])
    # GraphQL/`--jq`-flattened shape.
    assert is_support_issue([SUPPORT_LABEL])
    assert not is_support_issue([{"name": "Feature"}, "Improvement"])
    assert not is_support_issue([])
    assert not is_support_issue(None)
    assert label_names([{"name": "a"}, "b", {"no_name": 1}, 7]) == ["a", "b"]


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        # Hygiene: never add a support report to the code project or stamp
        # it with project fields.
        ("ensure_project_hygiene.yml", "add-to-project"),
        # Assignment: never hand one to the scrummaster.
        ("assign_backlog.yml", "assign"),
        # Release ledger: a support report is not release work.
        ("add-to-release-issue-on-milestone.yml", "add"),
    ],
)
def test_issue_workflows_skip_support_labeled_issues(workflow, job):
    condition = _job_conditions(workflow)[job]
    assert "github.event.issue.labels.*.name" in condition
    assert SUPPORT_LABEL in condition
    assert "!contains" in condition.replace(" ", "")


def test_the_agent_skip_is_keyed_on_the_guaranteed_label():
    """One name, guaranteed to exist, on every ticket, in every guard (#3811).

    The skip is only as good as the label being present, and the label is
    only present because something guarantees it. This asserts the two ends
    are the same string end to end -- the name the template declares, the
    name the ensure-label workflow creates and verifies, the name the intake
    guard restores, and the name every agent-side guard tests for.

    It is deliberately literal. #3810 leaked because the routing key was
    assumed present rather than guaranteed, and a later well-meant rename of
    any one of these would reproduce it exactly.
    """
    template = yaml.safe_load(
        (_REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "support.yml").read_text(encoding="utf-8")
    )
    assert template["labels"] == [SUPPORT_LABEL]

    ensure = yaml.safe_load(
        (_WORKFLOWS / "admin_ensure_support_label.yml").read_text(encoding="utf-8")
    )
    assert ensure["jobs"]["ensure-label"]["env"]["LABEL"] == SUPPORT_LABEL

    guard = yaml.safe_load((_WORKFLOWS / "support_intake_guard.yml").read_text(encoding="utf-8"))
    assert guard["jobs"]["repair-and-alert"]["env"]["LABEL"] == SUPPORT_LABEL

    # And the Python the selector/hygiene helpers share.
    assert is_support_issue([{"name": template["labels"][0]}])


def _page(labels: list[str], number: int = 4242) -> dict:
    """One project-items page holding a single open Backlog issue."""
    return {
        "data": {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "content": {
                                "__typename": "Issue",
                                "number": number,
                                "state": "OPEN",
                                "milestone": {"title": "Phase 6"},
                                "labels": {"nodes": [{"name": n} for n in labels]},
                            },
                            "fieldValues": {
                                "nodes": [
                                    {
                                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                        "field": {"name": "Status"},
                                        "name": "Backlog",
                                    }
                                ]
                            },
                        }
                    ],
                }
            }
        }
    }


def _summarize(page: dict, tmp_path: Path) -> dict:
    page_file = tmp_path / "page.json"
    page_file.write_text(json.dumps(page), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_LIB / "summarize_backlog_page.py"), str(page_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_a_support_issue_on_the_board_is_never_selected(tmp_path):
    summary = _summarize(_page([SUPPORT_LABEL]), tmp_path)
    assert summary["best_issue"] is None
    assert summary["backlog_open"] == 0
    # Nor does it inflate the open-work count: it is not work this loop can
    # ever do, so counting it would misreport the queue's depth.
    assert summary["open_issues"] == 0


def test_an_ordinary_issue_is_still_selected(tmp_path):
    """The guard must not swallow normal work -- the same page minus the label."""
    summary = _summarize(_page(["Feature"]), tmp_path)
    assert summary["best_issue"] == 4242
    assert summary["backlog_open"] == 1
    assert summary["open_issues"] == 1


def test_the_project_query_asks_for_labels():
    """The selector can only filter on the label if the query fetches it."""
    gh_project = (_REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh").read_text(
        encoding="utf-8"
    )
    query_start = gh_project.index("BACKLOG_PAGE_QUERY=")
    query = gh_project[query_start : gh_project.index("'", gh_project.index("'", query_start) + 1)]
    assert "labels(first:" in query
    assert "nodes { name }" in query


#: Everything that would couple agent code to the owner-run Support project.
#: Routing onto it is that project's own auto-add workflow; agents have no
#: access, by verification and by intent.
#:
#: These are *bindings* -- a project number an agent could call the API with,
#: or a configured handle for it. Naming the project in prose is fine and in
#: places necessary (the Support label's own description says where a report
#: is routed); what must not exist is code that can reach it.
_SUPPORT_PROJECT_BINDINGS = (
    "projects/5",
    "PROJECT_NUMBER=5",
    "PROJECT_NUMBER: 5",
    "PROJECT_NUMBER=${{ 5",
    "SUPPORT_PROJECT",
    "--project 5",
    "--project-number 5",
)
_COMMENT_PREFIXES = ("#", "//", "*", "--")


def test_no_agent_code_references_the_support_project():
    """Routing onto project 5 is that project's own auto-add workflow, not ours."""
    offenders = []
    for path in list(_WORKFLOWS.glob("*.yml")) + list(
        (_REPO_ROOT / "scripts" / "agents").rglob("*")
    ):
        if not path.is_file() or path.suffix in {".pyc", ".md"}:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            stripped = line.strip()
            # Prose explaining the boundary is fine; a real binding is not.
            if stripped.startswith(_COMMENT_PREFIXES):
                continue
            if any(binding in line for binding in _SUPPORT_PROJECT_BINDINGS):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, "agent code must not touch the Support project:\n" + "\n".join(offenders)
