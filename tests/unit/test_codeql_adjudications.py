#!/usr/bin/env python3
"""The adjudication register, and that a moving sink line cannot invalidate it (#4222).

The class this covers: an adjudication of a code-scanning finding used to live
only in GitHub's per-alert state, keyed to the sink's LINE NUMBER. Moving a
dismissed sink re-raised the identical accepted finding as a new open alert,
and nothing reported it until the release ceremony's gate refused to release
(#143 -> #144; #21/#22/#23 -> #145).

So the load-bearing test here is
`test_a_sink_that_moved_lines_still_matches`. The rest keep the register
honest: it has to parse, its entries have to name functions that really exist
in the tree, a new source into an accepted sink must NOT be auto-dismissed,
and "could not resolve the sink" must not read as "accepted".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agents" / "lib"))

import codeql_adjudications as adj  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. The register in the tree
# ---------------------------------------------------------------------------


def test_the_register_parses_and_is_not_empty() -> None:
    register = adj.load_register()

    assert register, "the register has no entries, so nothing is adjudicated"
    assert all(e.reason in adj.VALID_REASONS for e in register)


def test_every_entry_names_a_function_that_exists() -> None:
    """A register that rots is worse than none: it keeps asserting "examined,
    not a defect" about code that has since been renamed or deleted, and the
    matcher then silently stops matching while the alert comes back."""
    for entry in adj.load_register():
        path = REPO_ROOT / entry.path
        assert path.is_file(), f"{entry.key()}: {entry.path} does not exist"
        source = path.read_text(encoding="utf-8")
        assert (
            f"def {entry.sink}(" in source
        ), f"{entry.key()}: no `def {entry.sink}` in {entry.path}"


def test_every_entry_names_source_functions_that_exist() -> None:
    """The flow's other end, checked the same way. A source function that no
    longer exists means the accepted flow is gone -- the entry should have
    been deleted with it, not left to accept some future namesake."""
    tree = "\n".join(p.read_text(encoding="utf-8") for p in (REPO_ROOT / "src").rglob("*.py"))
    for entry in adj.load_register():
        for source in sorted(entry.sources):
            assert f"def {source}(" in tree, f"{entry.key()}: no `def {source}` under src/"


def test_every_entry_names_tests_that_exist() -> None:
    """`enforced_by` is the #4174 obligation: a rule this project states and
    does not enforce is itself a defect class. The named test must be real,
    or the entry is asserting an unchecked premise."""
    for entry in adj.load_register():
        for ref in entry.enforced_by:
            path_part, _, test_name = ref.partition("::")
            path = REPO_ROOT / path_part
            assert path.is_file(), f"{entry.key()}: enforced_by names a missing file {path_part}"
            assert test_name, f"{entry.key()}: enforced_by {ref!r} names no test"
            assert f"def {test_name}(" in path.read_text(
                encoding="utf-8"
            ), f"{entry.key()}: {path_part} has no `def {test_name}`"


def test_the_two_v301_blockers_are_adjudicated() -> None:
    """#144 and #145, the alerts that blocked v3.0.1, by their recorded history."""
    raised = {n for entry in adj.load_register() for n in entry.raised_as}

    assert {144, 145} <= raised


# ---------------------------------------------------------------------------
# 2. enclosing_function
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "subject.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_enclosing_function_finds_the_innermost_definition(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "def outer():\n" "    def inner():\n" "        print('sink')\n" "    return inner\n",
    )

    assert adj.enclosing_function(path, 3) == "inner"
    assert adj.enclosing_function(path, 4) == "outer"


def test_enclosing_function_reports_module_scope(tmp_path: Path) -> None:
    path = _write(tmp_path, "print('sink')\n")

    assert adj.enclosing_function(path, 1) == adj.MODULE_SCOPE


@pytest.mark.parametrize("body", ["def broken(:\n", ""])
def test_enclosing_function_cannot_tell_is_not_module_scope(tmp_path: Path, body: str) -> None:
    """A file that does not parse returns None, which matches nothing. The
    distinction matters: `<module>` is an answer, None is the absence of one,
    and an entry for `<module>` must not swallow an unparseable file."""
    path = _write(tmp_path, body)
    expected = None if body else adj.MODULE_SCOPE

    assert adj.enclosing_function(path, 1) is expected


def test_enclosing_function_on_a_missing_file_is_none(tmp_path: Path) -> None:
    assert adj.enclosing_function(tmp_path / "gone.py", 1) is None


# ---------------------------------------------------------------------------
# 3. Matching -- the class this issue is about
# ---------------------------------------------------------------------------

RULE = "py/clear-text-logging-sensitive-data"


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A miniature repo: a sink module and a source module."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "printer.py").write_text(
        "def _print_row(label, value):\n    print(f'{label} {value}')\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "creds.py").write_text(
        "def read_vnc_password():\n    return 'secret'\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def register(tmp_path: Path) -> list[adj.Adjudication]:
    path = tmp_path / "register.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "adjudications": [
                    {
                        "rule": RULE,
                        "path": "src/printer.py",
                        "sink": "_print_row",
                        "sources": ["read_vnc_password"],
                        "reason": "won't fix",
                        "comment": "Intended, on explicit operator request.",
                        "raised_as": [143],
                        "enforced_by": ["tests/unit/test_x.py::test_y"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return adj.load_register(path)


def _alert(number: int, line: int, severity: str = "high") -> dict:
    return {
        "number": number,
        "rule": {"id": RULE, "security_severity_level": severity},
        "most_recent_instance": {"location": {"path": "src/printer.py", "start_line": line}},
    }


def test_a_sink_that_moved_lines_still_matches(tree: Path, register: list) -> None:
    """THE regression test for #4222.

    #143 was dismissed at `cloud_deploy.py:3690`. `_print_row` moved to
    `:3924` and the identical flow came back as #144, two weeks before the
    v3.0.1 ceremony. Keyed to the function, both lines are the same finding --
    and so is any future line the function lands on.
    """
    flows = {(RULE, "src/printer.py", line): {"read_vnc_password"} for line in (1, 2)}

    for line in (1, 2):
        report = adj.match_alerts([_alert(144, line)], register, flows, repo_root=tree)

        assert not report.unadjudicated, f"line {line} was treated as a new finding"
        assert report.adjudicated[0][1].reason == "won't fix"


def test_a_new_source_into_an_accepted_sink_is_not_dismissed(tree: Path, register: list) -> None:
    """The register accepts a FLOW, not a sink. Accepting the sink outright
    would rebuild this issue's trap one level up: the next sensitive value
    routed through the same printer would be auto-dismissed unexamined."""
    flows = {(RULE, "src/printer.py", 2): {"read_aws_secret_key"}}

    report = adj.match_alerts([_alert(200, 2)], register, flows, repo_root=tree)

    assert report.unadjudicated
    assert len(report.blocking_unadjudicated) == 1


def test_an_alert_with_no_known_flow_is_not_dismissed(tree: Path, register: list) -> None:
    """No SARIF flow means the source was never verified. "Could not tell" is
    reported, never accepted -- the #3468 distinction."""
    report = adj.match_alerts([_alert(201, 2)], register, {}, repo_root=tree)

    assert len(report.blocking_unadjudicated) == 1


def test_a_different_rule_at_the_same_place_is_not_dismissed(tree: Path, register: list) -> None:
    alert = _alert(202, 2)
    alert["rule"]["id"] = "py/redos"
    flows = {("py/redos", "src/printer.py", 2): {"read_vnc_password"}}

    report = adj.match_alerts([alert], register, flows, repo_root=tree)

    assert len(report.blocking_unadjudicated) == 1


def test_a_renamed_sink_function_is_not_dismissed(tree: Path, register: list) -> None:
    """A rename is a deliberate change to the code, not a line shift, so the
    adjudication stops applying and a human is asked again."""
    (tree / "src" / "printer.py").write_text(
        "def _render_row(label, value):\n    print(f'{label} {value}')\n", encoding="utf-8"
    )
    flows = {(RULE, "src/printer.py", 2): {"read_vnc_password"}}

    report = adj.match_alerts([_alert(203, 2)], register, flows, repo_root=tree)

    assert len(report.blocking_unadjudicated) == 1


def test_a_sub_blocking_severity_is_reported_but_does_not_fail(tree: Path, register: list) -> None:
    """The release gate blocks on critical/high only, so nothing else may fail
    this check -- a medium alert that automation refused to pass would stop
    releases the ceremony would have allowed."""
    flows = {(RULE, "src/printer.py", 2): {"read_aws_secret_key"}}

    report = adj.match_alerts([_alert(204, 2, severity="medium")], register, flows, repo_root=tree)

    assert report.unadjudicated
    assert report.blocking_unadjudicated == []


# ---------------------------------------------------------------------------
# 4. Register validation, and the SARIF reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ({"reason": "because I said so"}, "is not one of"),
        ({"sources": []}, "missing `sources`"),
        ({"enforced_by": []}, "missing `enforced_by`"),
        ({"comment": ""}, "missing `comment`"),
        ({"sink": ""}, "missing `sink`"),
    ],
)
def test_a_malformed_entry_is_fatal(tmp_path: Path, mutation: dict, fragment: str) -> None:
    """Fatal, not skipped. A register that half-loads dismisses less than it
    should while reporting the rest as new, which is indistinguishable from
    the defect this replaced."""
    entry = {
        "rule": RULE,
        "path": "src/printer.py",
        "sink": "_print_row",
        "sources": ["read_vnc_password"],
        "reason": "won't fix",
        "comment": "ok",
        "enforced_by": ["tests/unit/test_x.py::test_y"],
    }
    entry.update(mutation)
    path = tmp_path / "register.yml"
    path.write_text(yaml.safe_dump({"adjudications": [entry]}), encoding="utf-8")

    with pytest.raises(adj.RegisterError, match=fragment):
        adj.load_register(path)


def test_a_missing_register_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(adj.RegisterError, match="no adjudication register"):
        adj.load_register(tmp_path / "absent.yml")


def test_two_entries_for_one_flow_are_rejected(tmp_path: Path) -> None:
    """Two entries for the same rule+sink are two answers to one question --
    CLAUDE.md's "one source per decision" at register scope."""
    entry = {
        "rule": RULE,
        "path": "src/printer.py",
        "sink": "_print_row",
        "sources": ["a"],
        "reason": "won't fix",
        "comment": "ok",
        "enforced_by": ["tests/unit/test_x.py::test_y"],
    }
    path = tmp_path / "register.yml"
    path.write_text(
        yaml.safe_dump({"adjudications": [entry, {**entry, "sources": ["b"]}]}), encoding="utf-8"
    )

    with pytest.raises(adj.RegisterError, match="adjudicated twice"):
        adj.load_register(path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ControlFlowNode for read_vnc_password()", "read_vnc_password"),
        (
            "ControlFlowNode for _glitchtip_secrets_doctor_issues()",
            "_glitchtip_secrets_doctor_issues",
        ),
        ("[post] ControlFlowNode for lines [List element]", "lines"),
        ("ControlFlowNode for Fstring", "Fstring"),
        ("ControlFlowNode for args [Attribute identity_file]", "args"),
        ("", None),
        ("ControlFlowNode for StringLiteral + something", None),
    ],
)
def test_step_symbol_reads_codeqls_own_labels(text: str, expected: str | None) -> None:
    """Lifted verbatim from this repo's SARIF (run 40040569431)."""
    assert adj.step_symbol(text) == expected


def _flow(line: int, text: str, uri: str = "src/creds.py") -> dict:
    return {
        "threadFlows": [
            {
                "locations": [
                    {
                        "location": {
                            "message": {"text": text},
                            "physicalLocation": {
                                "artifactLocation": {"uri": uri},
                                "region": {"startLine": line},
                            },
                        }
                    }
                ]
            }
        ]
    }


def _sarif(flows: list[dict]) -> dict:
    return {
        "runs": [
            {
                "results": [
                    {
                        "ruleId": RULE,
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "src/printer.py"},
                                    "region": {"startLine": 2},
                                }
                            }
                        ],
                        "codeFlows": flows,
                    }
                ]
            }
        ]
    }


def test_source_functions_are_read_from_every_flow(tree: Path) -> None:
    """Every `codeFlows` entry, not just the first: an alert stays open while
    any path lives, so covering `codeFlows[0]` alone would accept a flow that
    was never looked at."""
    sarif = _sarif(
        [
            _flow(1, "ControlFlowNode for read_vnc_password()"),
            _flow(2, "ControlFlowNode for read_aws_secret_key()"),
        ]
    )

    found = adj.source_functions_by_sink(sarif, repo_root=tree)

    assert found[(RULE, "src/printer.py", 2)] == {"read_vnc_password", "read_aws_secret_key"}


def test_the_symbol_label_beats_the_enclosing_function(tree: Path) -> None:
    """The flaw the first implementation shipped with, found by running it.

    #144's flows start at `cloud_screen.py:944`/`:983`, which enclose in
    `_print_open_summary`/`_print_status_summary` -- the CALLERS -- while the
    step is labelled `read_vnc_password()`. #145's starts inside `doctor`,
    which is also its own sink. Keyed on the enclosing function, neither
    matched; keyed on the label, both do.
    """
    sarif = _sarif([_flow(2, "ControlFlowNode for read_vnc_password()", uri="src/printer.py")])

    found = adj.source_functions_by_sink(sarif, repo_root=tree)

    assert found[(RULE, "src/printer.py", 2)] == {"read_vnc_password"}


def test_an_unlabelled_step_falls_back_to_its_enclosing_function(tree: Path) -> None:
    sarif = _sarif([_flow(2, "ControlFlowNode for a + b")])

    found = adj.source_functions_by_sink(sarif, repo_root=tree)

    assert found[(RULE, "src/printer.py", 2)] == {"read_vnc_password"}


def test_a_result_with_no_flows_is_skipped(tree: Path) -> None:
    sarif = {"runs": [{"results": [{"ruleId": RULE, "locations": [], "codeFlows": []}]}]}

    assert adj.source_functions_by_sink(sarif, repo_root=tree) == {}


def test_the_dismissal_comment_points_back_at_the_register(register: list) -> None:
    """The comment GitHub holds is a copy; the file is the original. A future
    session reading the alert has to be able to find the decision."""
    comment = adj.dismissal_comment(register[0])

    assert "security/codeql-adjudications.yml" in comment
    assert "#143" in comment
    assert "tests/unit/test_x.py::test_y" in comment
    assert register[0].comment in comment


def test_alert_severity_falls_back_to_the_rule_severity() -> None:
    assert adj.alert_severity({"rule": {"security_severity_level": "High"}}) == "high"
    assert adj.alert_severity({"rule": {"severity": "error"}}) == "error"
    assert adj.alert_severity({"rule": {}}) == ""
