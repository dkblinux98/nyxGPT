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

    # The form stays minimal, and ticket-type classification is the owner's
    # triage act on the Support project -- never a field the reporter picks.
    field_ids = {field.get("id") for field in template["body"] if field.get("id")}
    assert field_ids == {"what_happened", "version", "platform"}
    assert not any(
        "ticket" in str(field.get("id", "")).lower() or "type" in str(field.get("id", "")).lower()
        for field in template["body"]
    )


def test_the_support_label_has_a_creation_path():
    """A template naming a nonexistent label silently applies nothing.

    So the label cannot be a remembered manual step: an owner-dispatchable
    workflow creates it (idempotently), which is also the record of the
    owner's authorization to create it at all (CLAUDE.md forbids agents
    creating labels otherwise).
    """
    spec = yaml.safe_load(
        (_WORKFLOWS / "admin_ensure_support_label.yml").read_text(encoding="utf-8")
    )
    # `on:` parses as the YAML boolean True -- 1.1 semantics.
    assert "workflow_dispatch" in (spec.get("on") or spec.get(True))
    body = (_WORKFLOWS / "admin_ensure_support_label.yml").read_text(encoding="utf-8")
    assert "gh label create" in body
    assert "--force" in body, "label creation must be idempotent"
    assert SUPPORT_LABEL in body


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
