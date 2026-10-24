"""One place answers "is this issue accepted?" (#4204, ledger D-066).

Two halves, and the second is the one that keeps the first true.

**The module.** `scripts/agents/lib/acceptance_status.py` holds the
accepted-status vocabulary. Before it, four places decided acceptance from
four independently hard-coded lists of Status values and `Done` was in NONE
of them -- even though `Done` is a lane the loop writes itself
(`scripts/release_ceremony.sh` Phase 3 sets every closed issue in the
released milestone to it). So an issue that was accepted (`For Release`) and
then RELEASED read as "not accepted yet" to the promotion sweep, the
parked-blocked sweep, the drain gate's sprint scope and the sprint park
state -- permanently, because nothing ever moves an issue back out of `Done`.

**The guard.** A rule this project states but does not ENFORCE is itself a
defect class (#4174): `check_no_hardcoded_release_branch.sh` held its rule in
a script no workflow invoked. So the single-source property is a test that
runs in `ci-tests.yml` with the rest of pytest, not a sentence in a docstring
-- it fails the build if a script starts comparing a Status value to decide
acceptance on its own, which is exactly how the four copies accumulated.
"""

from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = ROOT / "scripts" / "agents" / "lib" / "acceptance_status.py"
_spec = importlib.util.spec_from_file_location("acceptance_status", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
acceptance_status = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = acceptance_status
_spec.loader.exec_module(acceptance_status)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("STATUS_ACCEPTANCE_TESTING", "STATUS_FOR_RELEASE", "STATUS_DONE"):
        monkeypatch.delenv(key, raising=False)


# --- the vocabulary --------------------------------------------------


class TestAccepted:
    def test_for_release_is_accepted(self):
        assert acceptance_status.is_accepted("For Release")

    def test_done_is_accepted(self):
        """THE defect of #4204. `Done` is the release ceremony's own
        close-out lane, and it was absent from every accepted-status list."""
        assert acceptance_status.is_accepted("Done")

    @pytest.mark.parametrize(
        "status",
        ["Backlog", "In Progress", "In Review", "Acceptance Testing", "Closed", "", None],
    )
    def test_everything_short_of_acceptance_is_not(self, status):
        assert not acceptance_status.is_accepted(status)

    def test_acceptance_failed_is_not_accepted(self):
        """It is "beyond" `Acceptance Testing` in board order and is still
        not accepted: the lane holds work the owner failed (#3780, D-042)."""
        assert not acceptance_status.is_accepted("Acceptance Failed")

    def test_surrounding_whitespace_is_tolerated(self):
        # issue_status() pipes a GraphQL field through `jq -r`; a stray
        # newline must not read as "not accepted".
        assert acceptance_status.is_accepted("  For Release\n")


class TestReachedAcceptance:
    @pytest.mark.parametrize("status", ["Acceptance Testing", "For Release", "Done"])
    def test_merged_and_at_or_past_the_acceptance_queue(self, status):
        assert acceptance_status.has_reached_acceptance(status)

    @pytest.mark.parametrize("status", ["Backlog", "In Progress", "In Review", "Closed", ""])
    def test_unmerged_lanes_have_not(self, status):
        assert not acceptance_status.has_reached_acceptance(status)

    def test_acceptance_failed_has_not_reached_acceptance(self):
        """Deliberate, and the reason #4204 consolidated two NAMED predicates
        rather than an ORDERING over lanes: the board's lanes are not a total
        order, and an ordering would have made `Acceptance Failed` -- where
        work is still owed on both populations that park there -- count as a
        completed blocker."""
        assert not acceptance_status.has_reached_acceptance("Acceptance Failed")

    def test_reached_is_a_superset_of_accepted(self):
        assert set(acceptance_status.accepted_statuses()) < set(
            acceptance_status.reached_acceptance_statuses()
        )


class TestStatusNamesAreConfigurable:
    def test_env_overrides_the_board_defaults(self, monkeypatch):
        monkeypatch.setenv("STATUS_FOR_RELEASE", "Shipped")
        monkeypatch.setenv("STATUS_DONE", "Released")
        assert acceptance_status.accepted_statuses() == ("Shipped", "Released")
        assert not acceptance_status.is_accepted("Done")
        assert acceptance_status.is_accepted("Released")

    def test_explicit_overrides_beat_the_environment(self, monkeypatch):
        """`sprint_calc.py` is handed its status names in its JSON payload,
        not in the environment, so it asks here with overrides instead of
        keeping a literal of its own."""
        monkeypatch.setenv("STATUS_FOR_RELEASE", "Shipped")
        assert acceptance_status.is_accepted("For Release", {"STATUS_FOR_RELEASE": "For Release"})

    def test_blank_override_falls_back_rather_than_matching_everything(self):
        # An empty payload value must not become an accepted status, or a
        # status-less item would read as accepted.
        assert acceptance_status.accepted_statuses({"STATUS_DONE": ""}) == ("For Release", "Done")
        assert not acceptance_status.is_accepted("", {"STATUS_DONE": ""})


class TestCli:
    """The shell asks through this CLI (`gh_project.sh` wraps it)."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(_MODULE_PATH), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_vocabulary_reports_both_lists(self):
        import json

        out = json.loads(self._run("vocabulary").stdout)
        assert out == {
            "accepted": ["For Release", "Done"],
            "reached_acceptance": ["Acceptance Testing", "For Release", "Done"],
        }

    def test_is_accepted_exit_codes(self):
        assert self._run("is-accepted", "Done").returncode == 0
        assert self._run("is-accepted", "Backlog").returncode == 1

    def test_has_reached_acceptance_exit_codes(self):
        assert self._run("has-reached-acceptance", "Acceptance Testing").returncode == 0
        assert self._run("has-reached-acceptance", "Acceptance Failed").returncode == 1

    def test_unknown_subcommand_is_a_usage_error_not_a_verdict(self):
        # Exit 2 must stay distinct from 1: a caller that read "unknown
        # subcommand" as "not accepted" would hold work back silently.
        assert self._run("whatever").returncode == 2
        assert self._run().returncode == 2


# --- the guard: nobody keeps a vocabulary of their own ---------------

#: The files allowed to compare an accepted-status value directly, each with
#: the reason it is not the class #4204 removed.
_ALLOWED = {
    # The vocabulary itself.
    "scripts/agents/lib/acceptance_status.py",
    # The release-ceremony TRIGGER asks a different question: "did the owner
    # just MOVE the release issue to `For Release`?" -- a transition, not a
    # state. It is deliberately exact and deliberately narrow, because what
    # it fires is irreversible (master fast-forward, tag, PyPI publish), and
    # a release issue already in `Done` is one whose ceremony has run.
    "scripts/agents/lib/ceremony_trigger.py",
}

#: A bash test-expression comparing against an accepted status, by variable
#: or by literal. `[[ "$x" != "$STATUS_FOR_RELEASE" ]]` was the promotion
#: sweep's gate; `"Done"` as a literal was the ceremony's writer.
_SH_COMPARE = re.compile(
    r"(?:==|!=)\s*\"?\$\{?STATUS_(?:FOR_RELEASE|DONE)\b"
    r"|\$\{?STATUS_(?:FOR_RELEASE|DONE)(?::-[^}]*)?\}?\"?\s*(?:==|!=)"
    r"|(?:==|!=)\s*\"(?:For Release|Done)\""
    r"|\"(?:For Release|Done)\"\s*(?:==|!=)"
)

#: WRITING an accepted status as a bare literal. The ceremony's Phase 3 did
#: exactly this (`set_issue_status "$n" "Done"`), which is why nothing tied
#: the writer of the lane to the readers' vocabulary. Setting it through
#: `"$STATUS_DONE"` / `"$STATUS_FOR_RELEASE"` is the correct form and is not
#: matched.
_SH_WRITE_LITERAL = re.compile(r"set_issue_status\s+\S+\s+\"(?:For Release|Done)\"")

#: Python deciding acceptance itself: comparing against a status name, or
#: KEYING a lookup by one. The second form is how `sprint_park_state` did it
#: -- `closed_by_status.get(status_for_release, [])` -- and a lookup excludes
#: `Done` just as silently as a comparison does.
_PY_NAME = r"\w*(?:for_release|status_done)\w*"
_PY_LITERAL = r"[\"'](?:For Release|Done)[\"']"
_PY_COMPARE = re.compile(
    rf"(?:==|!=)\s*(?:\w+\.)?(?:{_PY_NAME}|{_PY_LITERAL})"
    rf"|(?:\w+\.)?{_PY_NAME}\s*(?:==|!=)"
    rf"|{_PY_LITERAL}\s*(?:==|!=)"
    rf"|\.get\(\s*(?:{_PY_NAME}|{_PY_LITERAL})"
    rf"|\[\s*(?:{_PY_NAME}|{_PY_LITERAL})\s*\]"
)


def _sh_code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with whole-line comments dropped.

    Deliberately naive about trailing `# …` (stripping it would also cut a
    `#` inside a quoted string), so the guard can only ever MISS a commented
    mention -- never invent one. Every real occurrence this guard is about is
    code.
    """
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        out.append((i, line))
    return out


def _py_code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments and DOCSTRINGS blanked out, via `tokenize`.

    A docstring explaining the old `status != for_release` must not count as
    a violation of it -- this file and `acceptance_status.py` both quote it.
    Single-quoted strings stay visible on purpose: a status name used as a
    comparison operand or a dict key is a decision, and blanking every string
    would have made the literal half of the pattern unmatchable -- a guard
    that can see nothing, which is the #4174 failure mode.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
        pytest.fail(f"{path} does not tokenize")
    for tok in tokens:
        triple = tok.type == tokenize.STRING and tok.string.lstrip("rRbBfFuU")[:3] in ('"""', "'''")
        if tok.type != tokenize.COMMENT and not triple:
            continue
        (start_row, start_col), (end_row, end_col) = tok.start, tok.end
        for row in range(start_row, min(end_row, len(lines)) + 1):
            line = lines[row - 1]
            lo = start_col if row == start_row else 0
            hi = end_col if row == end_row else len(line)
            lines[row - 1] = line[:lo] + " " * max(0, hi - lo) + line[hi:]
    return [(i, line) for i, line in enumerate(lines, start=1) if line.strip()]


def _violations(suffix: str, patterns: tuple[re.Pattern[str], ...], reader) -> list[str]:
    found = []
    for path in sorted((ROOT / "scripts").rglob(f"*{suffix}")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        for lineno, line in reader(path):
            if any(pattern.search(line) for pattern in patterns):
                found.append(f"{rel}:{lineno}: {line.strip()}")
    return found


def test_no_shell_script_compares_an_accepted_status_itself():
    """`status_is_accepted` / `status_has_reached_acceptance` (gh_project.sh)
    are how the shell asks. A `[[ ... == "$STATUS_FOR_RELEASE" ]]` of its own
    is a fifth copy of the vocabulary, which is the class, not the
    instance."""
    violations = _violations(".sh", (_SH_COMPARE, _SH_WRITE_LITERAL), _sh_code_lines)
    assert not violations, (
        "These compare a Status value to decide acceptance instead of asking "
        "lib/acceptance_status.py (#4204, ledger D-066):\n  " + "\n  ".join(violations)
    )


def test_no_python_lib_compares_an_accepted_status_itself():
    violations = _violations(".py", (_PY_COMPARE,), _py_code_lines)
    assert not violations, (
        "These compare a Status value to decide acceptance instead of calling "
        "acceptance_status.is_accepted / has_reached_acceptance "
        "(#4204, ledger D-066):\n  " + "\n  ".join(violations)
    )


def test_the_guard_can_actually_see_the_pattern_it_forbids():
    """The failure mode of a grep guard is matching nothing at all (#4174):
    it passes forever and proves nothing. Both regexes are checked against
    the exact code #4204 removed."""
    assert _SH_COMPARE.search('    if [[ "$astatus" != "$STATUS_FOR_RELEASE" ]]; then')
    assert _SH_COMPARE.search(
        '  if [[ "$bstatus" != "$STATUS_ACCEPTANCE_TESTING" ]] '
        '&& [[ "$bstatus" != "$STATUS_FOR_RELEASE" ]]; then'
    )
    assert _SH_WRITE_LITERAL.search('      set_issue_status "$n" "Done"')
    assert not _SH_WRITE_LITERAL.search('      set_issue_status "$n" "${STATUS_DONE:-Done}"')
    assert not _SH_COMPARE.search('  set_issue_status "$feature" "$STATUS_FOR_RELEASE"')
    assert _PY_COMPARE.search("        if sprint == active_sprint and status != for_release:")
    assert _PY_COMPARE.search("    accepted = closed_by_status.get(status_for_release, [])")
    assert _PY_COMPARE.search('    accepted = closed_by_status["For Release"]')
    assert not _PY_COMPARE.search('        f"and is in **For Release** ({len(accepted)} items)"')


def test_the_release_ceremony_writes_done_through_the_named_status():
    """The WRITER is part of the class. `scripts/release_ceremony.sh` Phase 3
    is what puts an issue in `Done`; while it held a bare literal there was
    nothing tying it to the readers' vocabulary at all."""
    ceremony = (ROOT / "scripts" / "release_ceremony.sh").read_text(encoding="utf-8")
    assert 'set_issue_status "$n" "${STATUS_DONE:-Done}"' in ceremony


def test_load_config_names_the_done_status():
    """`STATUS_DONE` is an optional config key with a literal default, the
    same treatment its three siblings get, so no new repo variable is needed
    and a rename stays a config change."""
    lib = (ROOT / "scripts" / "agents" / "lib" / "gh_project.sh").read_text(encoding="utf-8")
    assert 'STATUS_DONE="${STATUS_DONE:-Done}"' in lib
