#!/usr/bin/env python3
"""The adjudication register, and that a moving sink line cannot invalidate it (#4222).

The class this covers: an adjudication of a code-scanning finding used to live
only in GitHub's per-alert state, keyed to the sink's LINE NUMBER. Moving a
dismissed sink re-raised the identical accepted finding as a new open alert,
and nothing reported it until the release ceremony's gate refused to release
(alert 143 -> 144; alerts 21/22/23 -> 145).

So the load-bearing test here is
`test_a_sink_that_moved_lines_still_matches`. The rest keep the register
honest: it has to parse, its entries have to name functions that really exist
in the tree, a new source into an accepted sink must NOT be auto-dismissed,
and "could not resolve the sink" must not read as "accepted".
"""

from __future__ import annotations

import re
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
    """Alerts 144 and 145, which blocked v3.0.1, by their recorded history."""
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
                        "headline": "Intended, on explicit operator request.",
                        "comment": "The long form of the same argument.",
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

    Alert 143 was dismissed at `cloud_deploy.py:3690`. `_print_row` moved to
    `:3924` and the identical flow came back as alert 144, two weeks before
    the v3.0.1 ceremony. Keyed to the function, both lines are the same finding --
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
        ({"headline": ""}, "missing `headline`"),
        ({"sink": ""}, "missing `sink`"),
        ({"headline": "x" * 300}, "GitHub allows 280"),
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
        "headline": "Short verdict.",
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
        "headline": "Short verdict.",
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
    # Bare number, and singular for one: `#143` would link to a deleted issue.
    assert "Raised as alert 143." in comment
    assert register[0].headline in comment
    # The full argument stays in the file: 280 characters cannot hold it, and
    # trying is how alert 119's dismissal ends mid-word.
    assert register[0].comment not in comment


def test_the_dismissal_comment_does_not_autolink_alert_numbers() -> None:
    """GitHub renders `dismissed_comment` as markdown, where `#143` is a link
    to ISSUE 143 -- and this repo's issues 21-146 are deleted, so the history
    line would point every reader at an unrelated HTTP 410. Alerts and issues
    share one `#N` namespace in rendered text, so the only safe form for an
    alert is the bare number. The register's own pointer keeps its `#4222`:
    that one IS an issue, and it resolves."""
    for entry in adj.load_register():
        comment = adj.dismissal_comment(entry)
        history = comment.partition("Raised as")[2]

        assert "#" not in history, f"{entry.key()}: alert number autolinks in {history!r}"
        for number in entry.raised_as:
            assert str(number) in history, f"{entry.key()}: alert {number} missing from history"


def test_every_live_entry_fits_githubs_comment_limit() -> None:
    """Learned by 422 against the real API: "Only 280 characters are allowed;
    1043 were supplied." A register that cannot be written is a register that
    dismisses nothing, and the failure arrives half-way through a run."""
    for entry in adj.load_register():
        written = len(adj.dismissal_comment(entry))

        assert written <= adj.MAX_DISMISSAL_COMMENT, f"{entry.key()}: {written} characters"


def test_alert_severity_falls_back_to_the_rule_severity() -> None:
    assert adj.alert_severity({"rule": {"security_severity_level": "High"}}) == "high"
    assert adj.alert_severity({"rule": {"severity": "error"}}) == "error"
    assert adj.alert_severity({"rule": {}}) == ""


# ---------------------------------------------------------------------------
# 7. The retired access premise (ledger S-011)
# ---------------------------------------------------------------------------

#: The sentence ledger **S-011** retires, in the spellings it was written in.
#: Measured false 2026-10-24 (#4222): the agent PAT these workflows carry
#: holds `repo`, which reaches `/code-scanning/alerts` and the
#: `application/sarif+json` analysis read directly.
RETIRED_ACCESS_CLAIMS = (
    "cannot call the code-scanning api",
    "can not call the code-scanning api",
    "cannot read the code-scanning api",
    "cannot call the code scanning api",
)

#: Where the retirement is *recorded*, which necessarily quotes it. The ledger
#: strikes the sentence through; this test names it to assert it is gone.
RETIREMENT_RECORDS = (
    Path("agents/LEDGER.md"),
    Path("tests/unit/test_codeql_adjudications.py"),
)

#: Directories with no prose of this project's own to falsify.
SKIPPED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
    ".venv",
    # Deliberately historical: a document whose purpose is to record what was
    # believed then cannot be falsified by a later measurement (CLAUDE.md,
    # owner reorganization 2026-07-31).
    "archived_product_docs",
}

SWEPT_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".sh", ".ts", ".tsx"}


def _swept_files() -> list[Path]:
    out = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SWEPT_SUFFIXES:
            continue
        if SKIPPED_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        out.append(path)
    return out


def test_no_file_still_asserts_the_retired_access_premise() -> None:
    """Ledger **S-011** retired "agent sessions cannot call the code-scanning
    API directly" and said do not restore it. Prose alone could not hold that:
    the first sweep corrected `review-runbook.md` §1f and `CLAUDE.md` and left
    the sentence standing in `apply_codeql_adjudications.py`'s `_gh` docstring
    -- inside the same PR that shipped S-011, citing CLAUDE.md for a claim
    CLAUDE.md had just stopped making -- and in `code_scan_report.yml`'s
    header. A retired premise is only retired where a guard says so, which is
    this one (CLAUDE.md: the fact goes in the guard that enforces it).

    `code_scan_report.yml` stays, and so does every instruction to dispatch
    it: the run URL is citable evidence a session's own read is not. What may
    not be said again is that the direct read does not work.
    """
    offenders = []
    for path in _swept_files():
        rel = path.relative_to(REPO_ROOT)
        if rel in RETIREMENT_RECORDS:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for claim in RETIRED_ACCESS_CLAIMS:
            if claim in text:
                offenders.append(f"{rel}: {claim!r}")

    assert (
        not offenders
    ), "ledger S-011 retired this sentence; it is still asserted in:\n" + "\n".join(offenders)


def test_markdown_does_not_write_alert_numbers_as_issue_links() -> None:
    """The **D-080** rendering rule, swept over every markdown file rather
    than the one entry it was noticed in. `#143` in markdown links to ISSUE
    143; this repo's issues 21-146 are deleted, so an alert written that way
    sends the reader to an unrelated HTTP 410. The quotation in
    `review-runbook.md` keeps the owner's exact words by putting them in a
    code span, which renders the characters and suppresses the link.

    Scoped to rendered surfaces on purpose. The same `#N` inside a workflow's
    YAML comment is displayed as source and links to nothing, so sweeping
    those would be the scope inflation `review-runbook.md` §1e warns against.
    """
    pattern = re.compile(r"(?:[Aa]lerts?|CodeQL) #\d{1,3}\b")
    offenders = []
    for path in _swept_files():
        if path.suffix != ".md":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

    assert not offenders, "write alert numbers bare (`alert 143`), not as `#143`:\n" + "\n".join(
        offenders
    )
