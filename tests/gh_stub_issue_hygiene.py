#!/usr/bin/env python3
"""Stateful `gh` stub for the issue-hygiene suite (#3816).

Stands in for the real CLI on PATH so tests/test_issue_hygiene.sh can run
scripts/agents/ensure_issue_hygiene.sh end to end without touching GitHub.
Three properties matter:

* **Stateful.** Every ``updateProjectV2ItemFieldValue`` mutation, label add
  and milestone edit is recorded and served back by subsequent reads. The
  assertions are therefore about the value a field actually ends up holding,
  not about which API strings were emitted.
* **Race-injecting.** ``race.json`` models a concurrent deliberate writer:
  after N reads of a project item's field values, the stub applies the
  owner's value to the board. That is the defect this suite exists for --
  hygiene checked a field, somebody else wrote it, and hygiene's default
  landed on top (#3814, 2026-08-16). Without injection the race is a
  coin flip that CI wins by luck.
* **Permissive.** Routes the flow touches incidentally (issue comment,
  auth status) return benign payloads, so a failure means the hygiene logic
  broke and not that a stub route was missing.

Fixtures the suite writes into ``$GH_STUB_DIR``:
  ``issue.json``      {"number": int, "title": str, "body": str,
                       "labels": [str], "milestone": str|null}
  ``board.json``      {"item_id": str|null, "fields": {name: value}}
                      item_id null == the issue is not on the board yet
  ``milestones.json`` [{"title": str, "state": str, "due_on": str}]
  ``iterations.json`` [{"id": str, "title": str, "startDate": "YYYY-MM-DD"}]
  ``race.json``       {"after_reads": int,        # project field-value reads
                       "after_issue_reads": int,  # REST issue reads
                       "fields": {name: value}, "milestone": str}
                      One trigger, either counter; both value keys optional.
  ``vanish.json``     {"after_reads": int}
                      The project item stops resolving after N field-value
                      reads: every later `node(id:)` query fails the way
                      GitHub really does, with "Could not resolve to a node
                      with the global id". Models the card being deleted or
                      the issue moved off the board mid-run (#3811).

Logs written into ``$GH_STUB_DIR``:
  ``gh.log``          every argv
  ``mutations.log``   "<field>\\t<value>" per project-field write
  ``issue_edit.log``  "<flag>\\t<value>" per `gh issue edit`
  ``comments.log``    issue comment bodies
  ``added.log``       project items created by addProjectV2ItemById
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

STUB_DIR = os.environ["GH_STUB_DIR"]
PROJECT_ID = "PVT_project"

#: Single-select options per field, mirroring the real board.
SELECT_OPTIONS = {
    "Status": [
        "Backlog",
        "In Progress",
        "In Review",
        "Acceptance Testing",
        "Acceptance Failed",
        "For Release",
        "Closed",
    ],
    "Priority": ["P0 - Critical", "P1 - High", "P2 - Medium", "P3 - Low"],
    "Effort": ["XS", "S", "M", "L", "XL"],
    # `Phase X` is the closure rule's target (#3871); the stub carries a
    # couple of real phases alongside it so "the option is missing" can be
    # tested by removing it, not by the field being absent entirely.
    "Phase": ["Phase 6", "Phase 7", "Phase X"],
    "Module": [
        "web-ui",
        "api",
        "rag",
        "cli",
        "tui",
        "testing",
        "documentation",
        "security",
        "sre",
    ],
}

DEFAULT_ITERATIONS = [
    {"id": "it::Sprint 7", "title": "Sprint 7", "startDate": "2020-01-01"},
    {"id": "it::Sprint 8", "title": "Sprint 8", "startDate": "2020-01-15"},
    {"id": "it::Sprint 9", "title": "Sprint 9", "startDate": "2099-01-01"},
]


def _path(name: str) -> str:
    return os.path.join(STUB_DIR, name)


def _read_json(name: str, default):
    try:
        with open(_path(name), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def _write_json(name: str, payload) -> None:
    with open(_path(name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


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
# Board / issue state
# --------------------------------------------------------------------------
def _state() -> dict:
    """Live state, seeded from the fixtures on first access."""
    state = _read_json("state.json", None)
    if state is not None:
        return state

    issue = _read_json("issue.json", {})
    board = _read_json("board.json", {})
    state = {
        "item_id": board.get("item_id"),
        "fields": dict(board.get("fields", {})),
        "labels": list(issue.get("labels", [])),
        "milestone": issue.get("milestone"),
        "assignees": list(issue.get("assignees", [])),
        "field_reads": 0,
        "issue_reads": 0,
        "race_applied": False,
    }
    _write_json("state.json", state)
    return state


def _save(state: dict) -> None:
    _write_json("state.json", state)


def _item_has_vanished(state: dict) -> bool:
    """Has the project item stopped resolving yet? (``vanish.json``, #3811)."""
    vanish = _read_json("vanish.json", None)
    if not vanish:
        return False
    return state["field_reads"] >= int(vanish.get("after_reads", 1))


def _emit_dangling_node_error() -> None:
    """Fail the way `gh` does on a project item id GitHub no longer knows.

    The real CLI exits non-zero and prints the GraphQL error as text on
    stderr -- which is why the caller cannot simply parse `.errors` -- and
    that text is the whole signal available to tell "this card is gone" apart
    from "the API is unhappy". Reproduce it verbatim; a tidier stub error
    would let a handler pass here and still die on the real thing.
    """
    sys.stderr.write(
        "gh: Could not resolve to a node with the global id "
        "'PVTI_lADOAlG7Cs4A2Ol1zgh3nLQ' (HTTP 200)\n"
    )
    sys.exit(1)


def _maybe_race(state: dict) -> None:
    """Apply the injected concurrent write once the read threshold is hit.

    Models the deliberate writer that hygiene raced and lost to: the value
    lands *between* hygiene's own check of a field and its write of that
    field, which is precisely the window the re-read guard closes.
    """
    race = _read_json("race.json", None)
    if not race or state.get("race_applied"):
        return
    if "after_issue_reads" in race:
        if state["issue_reads"] < int(race["after_issue_reads"]):
            return
    elif state["field_reads"] < int(race.get("after_reads", 1)):
        return

    for field, value in race.get("fields", {}).items():
        state["fields"][field] = value
    if race.get("milestone"):
        state["milestone"] = race["milestone"]
    state["race_applied"] = True
    _log(
        "race.log",
        f"applied after {state['field_reads']} field read(s) / "
        f"{state['issue_reads']} issue read(s)",
    )


# --------------------------------------------------------------------------
# GraphQL
# --------------------------------------------------------------------------
def _fields_payload() -> dict:
    # GH_STUB_NO_PHASE_X models a board whose Phase field exists but has no
    # `Phase X` option -- the case the closure rule must fail loudly on
    # rather than create the option (#3871).

    nodes = []
    for name, options in SELECT_OPTIONS.items():
        if name == "Phase" and os.environ.get("GH_STUB_NO_PHASE_X"):
            options = [o for o in options if o != "Phase X"]
        nodes.append(
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": f"PVTF::{name}",
                "name": name,
                "options": [{"id": f"opt::{name}::{o}", "name": o} for o in options],
            }
        )
    nodes.append(
        {
            "__typename": "ProjectV2IterationField",
            "id": "PVTF::Sprint",
            "name": "Sprint",
            "configuration": {"iterations": _read_json("iterations.json", DEFAULT_ITERATIONS)},
        }
    )
    return {"data": {"node": {"fields": {"nodes": nodes}}}}


def _field_values_payload(state: dict) -> dict:
    nodes = []
    for name, value in state["fields"].items():
        if value in (None, ""):
            continue
        if name == "Sprint":
            nodes.append(
                {
                    "__typename": "ProjectV2ItemFieldIterationValue",
                    "field": {"name": name},
                    "title": value,
                }
            )
        else:
            nodes.append(
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "field": {"name": name},
                    "name": value,
                }
            )
    return {"data": {"node": {"fieldValues": {"nodes": nodes}}}}


def _items_page(state: dict) -> dict:
    issue = _read_json("issue.json", {})
    nodes = []
    if state.get("item_id"):
        nodes.append(
            {
                "id": state["item_id"],
                "content": {"__typename": "Issue", "number": int(issue.get("number", 0))},
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


def _record_write(state: dict, query: str, params: dict) -> None:
    field = params.get("field", "").replace("PVTF::", "")
    value = ""
    for marker in ('singleSelectOptionId:"', 'iterationId:"'):
        if marker in query:
            raw = query.split(marker, 1)[1].split('"', 1)[0]
            value = raw.split("::")[-1]
            break
    else:
        if "text:" in query:
            value = query.split("text:", 1)[1].split("\n", 1)[0].strip().strip('"')

    state["fields"][field] = value
    _save(state)
    _log("mutations.log", f"{field}\t{value}")


def _graphql(query: str, params: dict, jq_filter: str | None) -> None:
    state = _state()

    if "updateProjectV2ItemFieldValue" in query:
        _record_write(state, query, params)
        _emit(
            {
                "data": {
                    "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": state.get("item_id")}}
                }
            },
            jq_filter,
        )

    if "clearProjectV2ItemFieldValue" in query:
        field = params.get("field", "").replace("PVTF::", "")
        state["fields"][field] = ""
        _save(state)
        _log("mutations.log", f"{field}\t(cleared)")
        _emit(
            {
                "data": {
                    "clearProjectV2ItemFieldValue": {"projectV2Item": {"id": state.get("item_id")}}
                }
            },
            jq_filter,
        )

    if "addProjectV2ItemById" in query:
        item_id = f"PVTI_added_{params.get('content', 'x')}"
        state["item_id"] = item_id
        _save(state)
        _log("added.log", item_id)
        _emit({"data": {"addProjectV2ItemById": {"item": {"id": item_id}}}}, jq_filter)

    if "projectV2(number" in query:
        _emit({"data": {"user": {"projectV2": {"id": PROJECT_ID}}}}, jq_filter)

    if "fields(first:100)" in query:
        _emit(_fields_payload(), jq_filter)

    # The existence probe (`project_item_state`): answers "is this card still
    # there?" without going through the graphql() wrapper, so it must be
    # served whether the item is live or gone.
    if "... on ProjectV2Item { id }" in query and "fieldValues" not in query:
        if _item_has_vanished(state):
            _emit_dangling_node_error()
        _emit({"data": {"node": {"id": state.get("item_id")}}}, jq_filter)

    if "fieldValues(first:50)" in query:
        if _item_has_vanished(state):
            _emit_dangling_node_error()
        payload = _field_values_payload(state)
        # Count the read, then let the injected writer land: the NEXT read of
        # this field (hygiene's guard, taken immediately before its write)
        # must see the deliberate value this one did not.
        state["field_reads"] += 1
        _maybe_race(state)
        _save(state)
        _emit(payload, jq_filter)

    if "items(first:100" in query:
        _emit(_items_page(state), jq_filter)

    _emit({"data": {}}, jq_filter)


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------
def _issue_payload(state: dict) -> dict:
    issue = _read_json("issue.json", {})
    number = int(issue.get("number", 0))
    return {
        "node_id": f"I_node_{number}",
        "number": number,
        "title": issue.get("title", ""),
        "body": issue.get("body", ""),
        "labels": [{"name": name} for name in state["labels"]],
        "milestone": ({"title": state["milestone"]} if state["milestone"] else None),
        # The closure rule (#3871) decides on these three.
        "state": issue.get("state", "open"),
        "state_reason": issue.get("state_reason"),
        "assignees": [{"login": login} for login in state["assignees"]],
    }


def _rest(route: str, jq_filter: str | None, params: dict, method: str) -> None:
    state = _state()
    # Match on the path only: the real API takes query strings (`?state=all`)
    # and a stub that folded them into the last path segment would silently
    # answer nothing, which reads as "the resource is empty" rather than
    # "this stub does not implement that call".
    parts = route.split("?", 1)[0].strip("/").split("/")

    if len(parts) >= 6 and parts[3] == "issues" and parts[5] == "assignees":
        for login in [v for k, v in params.items() if k.startswith("assignees")]:
            if method == "POST" and login not in state["assignees"]:
                state["assignees"].append(login)
                _log("assignees.log", f"+{login}")
            elif method == "DELETE" and login in state["assignees"]:
                state["assignees"].remove(login)
                _log("assignees.log", f"-{login}")
        _save(state)
        _emit({"assignees": [{"login": x} for x in state["assignees"]]}, jq_filter)

    if len(parts) >= 4 and parts[3] == "milestones":
        _emit(_read_json("milestones.json", []), jq_filter)

    if len(parts) >= 5 and parts[3] == "issues":
        payload = _issue_payload(state)
        # Same ordering as the field reads: this read is served the value it
        # had, and only then may the injected writer land -- so hygiene's
        # re-read (the one taken immediately before `gh issue edit
        # --milestone`) is the one that sees it.
        state["issue_reads"] += 1
        _maybe_race(state)
        _save(state)
        _emit(payload, jq_filter)

    _emit({}, jq_filter)


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------
def _issue_command(argv: list[str]) -> None:
    state = _state()
    action = argv[1] if len(argv) > 1 else ""

    if action == "edit":
        index = 2
        while index < len(argv):
            token = argv[index]
            if token == "--add-label":
                state["labels"].append(argv[index + 1])
                _log("issue_edit.log", f"--add-label\t{argv[index + 1]}")
                index += 2
            elif token == "--milestone":
                state["milestone"] = argv[index + 1]
                _log("issue_edit.log", f"--milestone\t{argv[index + 1]}")
                index += 2
            else:
                index += 1
        _save(state)
        sys.exit(0)

    if action == "comment":
        body = ""
        if "--body" in argv:
            body = argv[argv.index("--body") + 1]
        _log("comments.log", body.replace("\n", "\\n"))
        sys.exit(0)

    sys.exit(0)


def main() -> None:
    argv = sys.argv[1:]
    _log("gh.log", " ".join(argv))

    if not argv:
        sys.exit(0)

    command = argv[0]
    if command == "auth":
        sys.exit(0)
    if command == "issue":
        _issue_command(argv)
    if command != "api":
        sys.exit(0)

    rest = argv[1:]
    route = ""
    method = "GET"
    jq_filter = None
    params: dict[str, str] = {}
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--jq":
            jq_filter = rest[index + 1]
            index += 2
        elif token in ("-X", "--method"):
            method = rest[index + 1]
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
    _rest(route, jq_filter, params, method)


if __name__ == "__main__":
    main()
