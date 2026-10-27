"""Guards for the stale-suppression gate (`scripts/ci/stale_suppressions.py`, #4239).

This gate is the mechanism that was missing when two `braces` entries in
`web/audit-ci.jsonc` stopped suppressing anything on 2026-10-23 (the
advisory was withdrawn, not fixed) and the only report was a *warning* on
every green `security-scan` run. It fails the build instead.

The tests fall into three groups, and the third is the point:

1. Detection, against audit-ci's and pip-audit's real output shapes. The
   warning arrives ANSI-coloured, in six singular/plural forms, and a
   parser that only recognises the one form this repo happened to hit is a
   gate that goes blind the next time an entry is module- or
   advisory-scoped.
2. Classification, for pip-audit, where nothing upstream computes the
   answer: unsuppressed findings must still fail (the original gate) and a
   dead ignore entry must also fail (the new one), derived from a single
   unfiltered report.
3. WIRING. A gate nothing runs is the #4174 class -- a rule this project
   states and does not enforce -- so `web/package.json`'s `audit:ci` script
   and the `security-scan` workflow are asserted to route through the
   module. Both are one-line edits away from silently reverting to the
   warning-only behaviour that let #4239 happen.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "stale_suppressions", REPO_ROOT / "scripts" / "ci" / "stale_suppressions.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before execution: @dataclass resolves its own module out of
    # sys.modules, and an unregistered module makes that lookup return None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


# ---------------------------------------------------------------------------
# 1. Detection: audit-ci's "allowlisted but not found" warning
# ---------------------------------------------------------------------------

#: audit-ci's output for #4239's own case, captured verbatim from
#: `npm run audit:ci` on the branch that still carried both entries --
#: including the SGR escapes `console.warn(yellow, ...)` wraps it in, which a
#: hand-written fixture would have omitted and the parser would then have
#: failed on in production only.
REAL_WARNING = (
    "\x1b[33mConsider not allowlisting paths: "
    "GHSA-vfj7-8cjw-p6xm|@ducanh2912/next-pwa>fast-glob>micromatch>braces, "
    "GHSA-vfj7-8cjw-p6xm|eslint-config-next>@next/eslint-plugin-next>fast-glob>"
    "micromatch>braces.\x1b[0m\n"
    "\x1b[32mPassed npm security audit.\x1b[0m\n"
)


def test_the_real_captured_warning_names_both_dead_entries() -> None:
    """The #4239 regression itself: this output must not pass the gate."""
    assert gate.stale_allowlist_entries(REAL_WARNING) == [
        "GHSA-vfj7-8cjw-p6xm|@ducanh2912/next-pwa>fast-glob>micromatch>braces",
        "GHSA-vfj7-8cjw-p6xm|eslint-config-next>@next/eslint-plugin-next>"
        "fast-glob>micromatch>braces",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "Consider not allowlisting module: braces.",
        "Consider not allowlisting modules: braces, micromatch.",
        "Consider not allowlisting advisory: GHSA-aaaa-bbbb-cccc.",
        "Consider not allowlisting advisories: GHSA-aaaa-bbbb-cccc, GHSA-dddd-eeee-ffff.",
        "Consider not allowlisting path: GHSA-aaaa-bbbb-cccc|a>b.",
        "Consider not allowlisting paths: GHSA-aaaa-bbbb-cccc|a>b, GHSA-dddd-eeee-ffff|c>d.",
    ],
)
def test_every_form_of_the_warning_is_recognised(line: str) -> None:
    """All six forms audit-ci can print, singular and plural, per entry kind.

    This repo's entries are path-scoped by policy
    (`test_audit_ci_allowlist.py`), so only the last two forms can occur
    here *today*. The other four are matched anyway: audit-ci chooses the
    wording from the entry kind, and a gate that only understands the kind
    the last defect used would miss the next one in silence.
    """
    assert gate.stale_allowlist_entries(line)


def test_a_passing_audit_with_no_allowlist_is_clean() -> None:
    """The post-fix state: nothing allowlisted, nothing reported, gate green."""
    assert gate.stale_allowlist_entries("\x1b[32mPassed npm security audit.\x1b[0m\n") == []


@pytest.mark.parametrize(
    "line",
    [
        # audit-ci's *other* allowlist messages, for entries that ARE live.
        "\x1b[33mFound vulnerable allowlisted advisories: GHSA-aaaa-bbbb-cccc.\x1b[0m",
        "\x1b[33mFound vulnerable allowlisted modules: braces.\x1b[0m",
        "\x1b[33mFound vulnerable advisory paths:\x1b[0m",
        # A justification comment that happens to quote the warning text --
        # audit-ci echoes the config in `--report` mode.
        '"// audit-ci prints Consider not allowlisting paths when an entry dies"',
    ],
)
def test_a_live_entry_or_a_quoted_mention_is_not_read_as_stale(line: str) -> None:
    """The inverse bug: failing the build on an allowlist that still works.

    A false positive here is worse than the defect it guards -- it would
    turn `security-scan` red repo-wide for an entry doing its job, which
    `web/audit-ci.jsonc` records happening four times for other reasons.
    The last case is why the pattern requires the terminating `.` at
    end-of-line rather than matching the phrase anywhere.
    """
    assert gate.stale_allowlist_entries(line) == []


def test_the_warning_is_found_on_stderr_too() -> None:
    """`console.warn` writes to stderr; the caller concatenates both streams.

    Asserted because the gate reads `stdout + stderr` and a future
    refactor that reads only one would pass every stale allowlist.
    """
    assert gate.stale_allowlist_entries(REAL_WARNING)
    assert "Consider not allowlisting" in gate.strip_ansi(REAL_WARNING)


def test_strip_ansi_leaves_the_entry_text_intact() -> None:
    """Entries contain `|` and `>`; the escape strip must not touch them."""
    stripped = gate.strip_ansi(REAL_WARNING)
    assert "\x1b" not in stripped
    assert "GHSA-vfj7-8cjw-p6xm|@ducanh2912/next-pwa>fast-glob" in stripped


# ---------------------------------------------------------------------------
# 2. The gate's other off-switch: show-not-found
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config, enabled",
    [
        ('{"high": true, "allowlist": []}', True),
        ('{"high": true, "show-not-found": true, "allowlist": []}', True),
        ('{"high": true, "show-not-found": false, "allowlist": []}', False),
        ('// a comment\n{"high": true, "show-not-found": false}', False),
    ],
)
def test_turning_off_audit_cis_reporting_is_detected(config: str, enabled: bool) -> None:
    """`show-not-found: false` would make the gate pass on a dead allowlist.

    audit-ci computes the not-found list either way and just stops printing
    it, so the gate has nothing to read -- a one-word config change that
    looks like a display preference and disables the whole mechanism.
    """
    assert gate.allowlist_reporting_is_enabled(config) is enabled


def test_the_live_config_has_not_turned_it_off() -> None:
    """...and the committed config has it on."""
    assert gate.allowlist_reporting_is_enabled(gate.AUDIT_CI_CONFIG.read_text()) is True


# ---------------------------------------------------------------------------
# 3. pip-audit: the ignore file and the classification
# ---------------------------------------------------------------------------


def test_the_ignore_file_parse_skips_comments_and_blanks() -> None:
    """The documented format: justification above, one ID per line."""
    text = (
        "# header\n"
        "\n"
        "# no upstream fix -- accepted 2026-01-01\n"
        "PYSEC-2026-1\n"
        "  GHSA-aaaa-bbbb-cccc  \n"
        "CVE-2026-1 # dev-only tool\n"
    )

    assert gate.parse_ignore_file(text) == [
        "PYSEC-2026-1",
        "GHSA-aaaa-bbbb-cccc",
        "CVE-2026-1",
    ]


def test_the_committed_ignore_file_parses() -> None:
    """Whatever it holds today, the gate can read it (it is empty as shipped)."""
    assert gate.parse_ignore_file(gate.PIP_AUDIT_IGNORE.read_text()) == []


def _report(*vulns: tuple[str, list[str]]) -> dict:
    return {
        "dependencies": [
            {"name": "pkg", "version": "1.0", "vulns": [{"id": i, "aliases": a}]} for i, a in vulns
        ]
    }


#: How `classify_pip_audit` labels a finding: the culprit, then the ID. One ID
#: can be reported against several packages, so the ID alone names neither.
def _label(identifier: str) -> str:
    return f"pkg 1.0: {identifier}"


def test_an_unsuppressed_finding_fails() -> None:
    """The original gate, preserved: a finding nobody accepted is blocking."""
    verdict = gate.classify_pip_audit(_report(("PYSEC-2026-1", [])), [])

    assert verdict.unsuppressed == [_label("PYSEC-2026-1")]
    assert not verdict.ok


def test_a_matching_ignore_entry_suppresses_and_passes() -> None:
    verdict = gate.classify_pip_audit(_report(("PYSEC-2026-1", [])), ["PYSEC-2026-1"])

    assert verdict.ok
    assert verdict.live_ignores == ["PYSEC-2026-1"]
    assert verdict.stale_ignores == []


def test_an_ignore_entry_that_matches_nothing_fails() -> None:
    """#4239's class, on the Python side: the entry suppresses nothing.

    pip-audit itself is silent about this -- `--ignore-vuln` accepts an ID
    that matches no finding without a word -- which is why the gate
    classifies an unfiltered report rather than reading pip-audit's verdict
    the way it reads audit-ci's.
    """
    verdict = gate.classify_pip_audit(_report(("PYSEC-2026-1", [])), ["GHSA-dead-dead-dead"])

    assert verdict.stale_ignores == ["GHSA-dead-dead-dead"]
    assert not verdict.ok


def test_an_ignore_written_as_an_alias_is_live_not_stale() -> None:
    """The ignore file documents PYSEC-, GHSA- and CVE- ids interchangeably.

    pip-audit reports whichever namespace its source used and lists the
    rest as `aliases`. Matching only the primary `id` would declare a
    working suppression dead and fail the build on a correct entry.
    """
    report = _report(("PYSEC-2026-1", ["CVE-2026-9999", "GHSA-aaaa-bbbb-cccc"]))

    verdict = gate.classify_pip_audit(report, ["GHSA-aaaa-bbbb-cccc"])

    assert verdict.ok
    assert verdict.live_ignores == ["GHSA-aaaa-bbbb-cccc"]


def test_an_empty_report_and_an_empty_ignore_file_pass() -> None:
    """The shipped state: nothing found, nothing accepted, nothing stale."""
    assert gate.classify_pip_audit({"dependencies": []}, []).ok


def test_a_dependency_with_a_null_vulns_list_is_tolerated() -> None:
    """pip-audit emits `"vulns": null` for some sources; `or []` covers it."""
    assert gate.reported_findings({"dependencies": [{"name": "p", "vulns": None}]}) == []


def test_one_finding_cannot_fail_the_build_under_its_own_alias() -> None:
    """The inverse of the test above, and the bug it was written against.

    An early draft collected identifiers into a flat set, so suppressing
    `GHSA-aaaa-bbbb-cccc` left its own `PYSEC-`/`CVE-` names in the
    unsuppressed list -- the accepted finding failing the build under a
    second name. Suppression is a property of the finding, not of the
    string the ignore file happens to spell.
    """
    report = _report(("PYSEC-2026-1", ["CVE-2026-9999", "GHSA-aaaa-bbbb-cccc"]))

    assert gate.classify_pip_audit(report, ["GHSA-aaaa-bbbb-cccc"]).unsuppressed == []


# ---------------------------------------------------------------------------
# 4. Wiring: the gate is actually what runs (#4174)
# ---------------------------------------------------------------------------

GATE_PATH = "scripts/ci/stale_suppressions.py"


def test_the_web_audit_script_routes_through_the_gate() -> None:
    """`npm run audit:ci` must be the strict gate, not bare audit-ci.

    This is the command CI runs, the one `agents/runbooks/developer-runbook.md`
    and `security/README.md` tell a human to run, and the one the #4239
    warning was printed by for four days without failing anything. Reverting
    it to `audit-ci --config audit-ci.jsonc` restores that blindness in one
    line, so the line is asserted.
    """
    scripts = json.loads((REPO_ROOT / "web" / "package.json").read_text())["scripts"]

    assert GATE_PATH.rsplit("/", 1)[-1] in scripts["audit:ci"], (
        f"web/package.json's audit:ci no longer routes through {GATE_PATH}, so a "
        "stale allowlist entry is a warning again rather than a failure (#4239)."
    )
    assert "npm-audit" in scripts["audit:ci"]


def test_the_security_scan_workflow_uses_both_halves_of_the_gate() -> None:
    """Both scanner steps go through the gate, by the commands they run.

    Read as text rather than parsed YAML on purpose: what matters is the
    command line that executes, and `security-scan.yml` is the only place
    either scanner is invoked in CI.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "security-scan.yml").read_text()

    assert f"{GATE_PATH} pip-audit" in workflow, (
        "security-scan.yml no longer runs the pip-audit half of the gate, so a "
        f"dead entry in security/pip-audit-ignore.txt is silent again. Restore "
        f"`python3 {GATE_PATH} pip-audit`."
    )
    assert "npm run audit:ci" in workflow, (
        "security-scan.yml no longer runs `npm run audit:ci`, which is where "
        "the npm half of the gate is wired (web/package.json)."
    )
    # Comment lines are excluded: this workflow explains in prose why the
    # bare invocation was replaced, and a check that cannot tell the
    # explanation from the command would forbid recording the reason.
    executable = [line for line in workflow.splitlines() if not line.strip().startswith("#")]
    direct = [
        line
        for line in executable
        if GATE_PATH not in line and re.search(r"(?:^|[;&|]\s*|run:\s*)pip-audit\b", line.strip())
    ]
    assert not direct, (
        f"security-scan.yml invokes `pip-audit` directly again: {direct}. A bare "
        "call cannot report an ignore entry that matches nothing -- that is the "
        "step #4239 replaced. Route it through the gate."
    )
    assert "--ignore-vuln" not in "\n".join(executable), (
        "security-scan.yml passes --ignore-vuln again. pip-audit accepts an ID "
        "that matches no finding without a word, which is what hid the dead "
        "entries in #4239; the gate classifies an unfiltered report instead."
    )


def test_the_workflow_still_proves_the_gate_has_teeth() -> None:
    """The fault-injection step is part of the gate, not decoration (#3753).

    Every other step in `security-scan.yml` passes on a clean tree whether
    or not this gate works -- which is precisely how #4239 survived four
    green runs. The injection step re-arms the defect and requires the gate
    to fail on it *by its own message*, so deleting the step would restore
    a scan that cannot distinguish a working gate from an absent one.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "security-scan.yml").read_text()

    assert "Prove the stale-suppression gate fails on a dead entry" in workflow, (
        "security-scan.yml no longer injects a dead suppression entry, so "
        "nothing in CI distinguishes a working stale-suppression gate from an "
        "absent one (#4239). Restore the fault-injection step."
    )
    assert workflow.count("STALE SUPPRESSION") >= 2, (
        "the fault-injection step must assert the gate's own message for BOTH "
        "halves -- a bare non-zero exit would also be produced by a real "
        "finding and proves nothing about the stale-entry path."
    )


def test_the_gate_offers_exactly_the_two_registers_it_wires() -> None:
    """A third scanner must wire its own step, not ride on a silent default."""
    with pytest.raises(SystemExit):
        gate.main(["not-a-register"])
