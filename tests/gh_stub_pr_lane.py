#!/usr/bin/env python3
"""Stateful `gh` stub for the PR-lane hygiene suite (#3742).

Stands in for the real CLI on PATH so tests/test_pr_lane_hygiene.sh can run
the merge flow, the close handler and the sweep end to end without touching
GitHub. Two properties matter:

* **Stateful.** Every ``updateProjectV2ItemFieldValue`` mutation is recorded
  in ``$GH_STUB_DIR/state.json`` and subsequent reads (a PR's Status, the
  project item page) serve the written value. That is what makes the lane
  invariant genuinely testable: the suite merges a PR and then *reads* the
  PR's Status back through the same helpers production uses, instead of
  asserting that a mutation string was emitted.
* **Permissive.** Routes the flows touch incidentally (issue close, comments,
  assignees, repository_dispatch) return benign payloads, so a test failure
  means the lane logic broke and not that a stub route was missing.

Fixtures the suite writes into ``$GH_STUB_DIR``:
  ``pr_items.json``   {pr_number: {"item_id": str|null, "status": str|null}}
  ``pulls.json``      {pr_number: {"head": str, "base": str, "merged": bool,
                                   "state": "open"|"closed",
                                   "head_sha": str, "base_sha": str}}
                      head_sha/base_sha feed #3862's closure gate, which
                      refuses to close an issue unless the PR's content is
                      verifiably on the base branch. They default to HEAD of
                      the checkout the suite runs in, which trivially
                      verifies; point head_sha at something unresolvable to
                      exercise the refusal.
  ``items_page.json``  project item nodes for the sweep query (see _sweep_page)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

STUB_DIR = os.environ["GH_STUB_DIR"]
PROJECT_ID = "PVT_project"
STATUS_FIELD_ID = "PVTSSF_status"

#: Board Status options, mirroring the real single-select field.
OPTIONS = {
    "Backlog": "opt_backlog",
    "In Progress": "opt_in_progress",
    "In Review": "opt_in_review",
    "Acceptance Testing": "opt_acceptance_testing",
    "Acceptance Failed": "opt_acceptance_failed",
    "For Release": "opt_for_release",
    "Closed": "opt_closed",
}
OPTION_NAMES = {v: k for k, v in OPTIONS.items()}


def _path(name: str) -> str:
    return os.path.join(STUB_DIR, name)


def _read_json(name: str, default):
    try:
        with open(_path(name), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def _state() -> dict:
    """item_id -> currently written Status option id."""
    return _read_json("state.json", {})


def _write_state(state: dict) -> None:
    with open(_path("state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def _log(name: str, line: str) -> None:
    with open(_path(name), "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _emit(payload, jq_filter: str | None) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    if not jq_filter:
        sys.stdout.write(text + "\n")
        sys.exit(0)
    result = subprocess.run(
        ["jq", "-r", jq_filter],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)


# --------------------------------------------------------------------------
# GraphQL
# --------------------------------------------------------------------------
def _status_of(item_id: str | None, fallback: str | None) -> str | None:
    if item_id and item_id in _state():
        return OPTION_NAMES.get(_state()[item_id])
    return fallback


def _fields_payload() -> dict:
    single_select = [
        {
            "__typename": "ProjectV2SingleSelectField",
            "id": STATUS_FIELD_ID,
            "name": "Status",
            "options": [{"id": oid, "name": name} for name, oid in OPTIONS.items()],
        }
    ]
    for name in ("Priority", "Effort", "Module"):
        single_select.append(
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": f"PVTSSF_{name.lower()}",
                "name": name,
                "options": [],
            }
        )
    return {"data": {"node": {"fields": {"nodes": single_select}}}}


def _pr_item_payload(pr_number: str, with_fields: bool) -> dict:
    entry = _read_json("pr_items.json", {}).get(str(pr_number))
    if not entry or not entry.get("item_id"):
        return {"data": {"repository": {"pullRequest": {"projectItems": {"nodes": []}}}}}

    node: dict = {"id": entry["item_id"], "project": {"id": PROJECT_ID}}
    if with_fields:
        status = _status_of(entry["item_id"], entry.get("status"))
        values = []
        if status:
            values.append({"field": {"name": "Status"}, "name": status})
        node["fieldValues"] = {"nodes": values}
    return {"data": {"repository": {"pullRequest": {"projectItems": {"nodes": [node]}}}}}


def _sweep_page() -> dict:
    """Project items page, with live Status from the mutation state."""
    nodes = []
    for raw in _read_json("items_page.json", []):
        status = _status_of(raw["item_id"], raw.get("status"))
        field_values = []
        if status:
            field_values.append(
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "field": {"name": "Status"},
                    "name": status,
                }
            )
        content = {"__typename": raw["type"], "number": raw["number"]}
        if raw["type"] == "PullRequest":
            content["state"] = raw["state"]
            content["title"] = raw.get("title", f"PR {raw['number']}")
        nodes.append(
            {
                "id": raw["item_id"],
                "content": content,
                "fieldValues": {"nodes": field_values},
            }
        )
    return {
        "data": {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                }
            }
        }
    }


def _graphql(query: str, params: dict, jq_filter: str | None) -> None:
    if "updateProjectV2ItemFieldValue" in query:
        item_id = params.get("item", "")
        option_id = ""
        marker = 'singleSelectOptionId:"'
        if marker in query:
            option_id = query.split(marker, 1)[1].split('"', 1)[0]
        state = _state()
        state[item_id] = option_id
        _write_state(state)
        _log("mutations.log", f"{item_id}\t{option_id}\t{OPTION_NAMES.get(option_id, '?')}")
        _emit(
            {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item_id}}}},
            jq_filter,
        )

    if "addProjectV2ItemById" in query:
        added = f"PVTI_added_{params.get('content', 'x')}"
        _log("added.log", added)
        _emit({"data": {"addProjectV2ItemById": {"item": {"id": added}}}}, jq_filter)

    if "projectV2(number" in query:
        _emit({"data": {"user": {"projectV2": {"id": PROJECT_ID}}}}, jq_filter)

    if "fields(first:100)" in query:
        _emit(_fields_payload(), jq_filter)

    if "pullRequest(number:$num)" in query:
        _emit(_pr_item_payload(params.get("num", ""), "fieldValues" in query), jq_filter)

    if "on PullRequest" in query and "items(first:100" in query:
        _emit(_sweep_page(), jq_filter)

    if "items(first:100" in query:
        # ensure_issue_in_project's scan: the issue already has an item.
        issue_item = f"PVTI_issue_{params.get('after') or 'x'}"
        _emit(
            {
                "data": {
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": issue_item,
                                    "content": {
                                        "__typename": "Issue",
                                        "number": int(os.environ.get("STUB_ISSUE", "0") or 0),
                                    },
                                }
                            ],
                        }
                    }
                }
            },
            jq_filter,
        )

    _emit({"data": {}}, jq_filter)


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------
def _rest(route: str, jq_filter: str | None) -> None:
    # Routes are repos/{owner}/{repo}/{resource}/{number}[/...].
    parts = route.strip("/").split("/")

    if "dependencies" in parts:
        _emit([], jq_filter)

    if len(parts) >= 5 and parts[3] == "pulls":
        pr = parts[4]
        entry = _read_json("pulls.json", {}).get(
            pr, {"head": "feat/example", "base": "v3.0.0", "merged": False, "state": "open"}
        )
        default_sha = os.environ.get("STUB_HEAD_SHA", "HEAD")
        _emit(
            {
                "node_id": f"PR_node_{pr}",
                "number": int(pr),
                "head": {
                    "ref": entry["head"],
                    "sha": entry.get("head_sha", default_sha),
                    "repo": {"full_name": "dkblinux98/nyxGPT"},
                },
                "base": {"ref": entry["base"], "sha": entry.get("base_sha", default_sha)},
                "mergeable": True,
                "mergeable_state": "clean",
                "merged": entry["merged"],
                "state": entry["state"],
            },
            jq_filter,
        )

    if len(parts) >= 5 and parts[3] == "branches":
        branch = "/".join(parts[4:])
        if branch == os.environ.get("STUB_BASE_BRANCH", "v3.0.0"):
            _emit({"name": branch}, jq_filter)
        sys.exit(1)  # head branch already deleted

    if len(parts) >= 5 and parts[3] == "issues":
        if parts[-1] == "comments":
            _log("comments.log", route)
            _emit([], jq_filter)
        _emit(
            {
                "node_id": f"I_node_{parts[4]}",
                "number": int(parts[4]),
                "assignees": [{"login": os.environ.get("STUB_OWNER", "dkblinux98")}],
                "milestone": None,
                "body": "",
                "state": "closed",
            },
            jq_filter,
        )

    _emit({}, jq_filter)


def main() -> None:
    argv = sys.argv[1:]
    _log("gh.log", " ".join(argv))

    if not argv:
        sys.exit(0)

    command = argv[0]
    if command in ("auth", "pr", "issue"):
        # auth status / pr merge / pr comment / issue close / issue comment:
        # nothing to model beyond "it worked".
        sys.exit(0)
    if command != "api":
        sys.exit(0)

    rest = argv[1:]
    route = ""
    jq_filter = None
    params: dict[str, str] = {}
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--jq":
            jq_filter = rest[index + 1]
            index += 2
        elif token in ("-X", "--method"):
            index += 2
        elif token in ("-f", "-F", "--field", "--raw-field"):
            key, _, value = rest[index + 1].partition("=")
            params[key] = value
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            if not route:
                route = token
            index += 1

    if route == "graphql":
        _graphql(params.get("query", ""), params, jq_filter)
    _rest(route, jq_filter)


if __name__ == "__main__":
    main()
