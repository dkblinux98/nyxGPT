#!/usr/bin/env python3
"""Fail the build when a suppression stops suppressing anything (#4239).

WHY THIS EXISTS. A suppression register -- `web/audit-ci.jsonc`'s
`allowlist`, `security/pip-audit-ignore.txt`,
`security/codeql-adjudications.yml` -- records a finding this project
examined and accepted, together with the reason and the condition that
would retire the entry. The entry is only as valid as that reason, and the
reason can be invalidated by events the recorded condition does not watch.

#4239 is the worked example. Two `braces` entries were accepted on
2026-10-04 (#4150) because GHSA-vfj7-8cjw-p6xm had no patched release, and
their REVISIT condition was "drop them when braces publishes a fix, or when
the two parents move off the fast-glob chain". Neither happened. On
2026-10-23 GitHub *withdrew* the advisory ("not a practically exploitable
vulnerability") and `npm audit` went quiet -- an outcome the condition did
not name. From then on the entries suppressed nothing, both dependency
paths still existed, and the only report was `audit-ci` printing "Consider
not allowlisting paths" as a *warning* on every green run. It stayed there
for four days and was found by a sweep on an unrelated issue.

That is the class: a suppression that suppresses nothing is a warning at
worst, so nothing in the build notices, and a dead entry keeps asserting
"examined, not a defect" about a finding nobody can re-check. Two of the
three registers already fail the build on it --
`tests/unit/test_codeql_adjudications.py::test_every_entry_names_a_function_that_exists`
for the CodeQL register, and
`tests/unit/test_npm_dependency_floors.py::test_no_deliberate_exact_pin_is_stale`
for `DELIBERATE_EXACT_PINS`. The two scanner registers did not, because
"does this entry still match?" is a question only the live scanner can
answer. This module is where that question is asked, once, for both of
them.

ONE SOURCE PER DECISION. The rule ("an entry that matches nothing fails the
build") and its message live here; only the *detection* differs per tool,
because the two scanners report differently:

  npm-audit   `audit-ci` already computes the answer and prints it. The
              gate runs it, passes its output and exit code straight
              through, and additionally fails on any "Consider not
              allowlisting ..." line. `web/package.json`'s `audit:ci`
              script routes through here, so every caller -- CI, the
              developer runbook's local command, the release ceremony --
              gets the strict behaviour without having to remember a flag.
  pip-audit   computes nothing of the kind: an unmatched `--ignore-vuln`
              is silently accepted. So the gate runs `pip-audit` ONCE with
              no ignores at all and does the classification itself, which
              answers both questions from the same report -- which findings
              are unsuppressed (the original gate) and which ignore entries
              matched nothing (the new one).

Both subcommands exit 0 on pass and 1 on failure, and print the scanner's
own output first so a failure is diagnosable from the log alone.

`tests/unit/test_stale_suppressions.py` covers the pure functions against
captured scanner output, and asserts the wiring -- that `audit:ci` and the
`security-scan` workflow still route through here -- because a gate that
can be quietly bypassed is the same defect one level up (#4174).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
AUDIT_CI_CONFIG = WEB_DIR / "audit-ci.jsonc"
PIP_AUDIT_IGNORE = REPO_ROOT / "security" / "pip-audit-ignore.txt"

#: `audit-ci` colours its warnings with `console.warn(yellow, message)`, so the
#: line arrives wrapped in SGR escapes. Strip them before matching rather than
#: trying to spell them in the pattern.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: Every form of audit-ci's "allowlisted but not found" warning. It prints one
#: line per *kind* of entry (module / advisory / path), singular or plural
#: depending on the count, and joins the entries with ", ". Matching all six
#: forms matters: this repo's entries are path-scoped by policy
#: (`tests/unit/test_audit_ci_allowlist.py`), but a module- or advisory-scoped
#: entry going stale is the same defect and must not slip past the pattern.
_NOT_FOUND = re.compile(
    r"Consider not allowlisting "
    r"(?:module|modules|advisory|advisories|path|paths): "
    r"(?P<entries>.+?)\.\s*$"
)

#: pip-audit's ignore file: one vulnerability ID per line, `#` comments and
#: blank lines skipped. The CI step used to parse this inline in bash; the
#: parse lives here now so the gate and its tests read the same file the same
#: way.
_IGNORE_ID = re.compile(r"^(?P<id>[^#\s]+)")


def strip_ansi(text: str) -> str:
    """Remove SGR colour escapes so the warning text can be matched."""
    return _ANSI.sub("", text)


def stale_allowlist_entries(output: str) -> list[str]:
    """Return the allowlist entries `audit-ci` reports as matching nothing.

    Reads audit-ci's own verdict rather than recomputing it: the tool knows
    which entries it applied, and parsing its answer cannot disagree with
    it. Returns a sorted, de-duplicated list so a caller can name every
    dead entry in one message; empty means every entry is still live (or
    there are none).
    """
    entries: set[str] = set()
    for line in strip_ansi(output).splitlines():
        match = _NOT_FOUND.search(line.strip())
        if match:
            entries.update(part.strip() for part in match["entries"].split(",") if part.strip())
    return sorted(entries)


def allowlist_reporting_is_enabled(config_text: str) -> bool:
    """True unless the config turns audit-ci's not-found reporting off.

    `--show-not-found` defaults to true, and the whole `npm-audit` gate
    rests on it: with `"show-not-found": false` audit-ci computes the dead
    entries and says nothing, so the gate would pass on a stale allowlist
    forever. Flipping it looks like a display preference in a diff, which
    is exactly why it is asserted rather than assumed.
    """
    stripped = re.sub(r"^\s*//.*$", "", config_text, flags=re.MULTILINE)
    return json.loads(stripped).get("show-not-found", True) is not False


def parse_ignore_file(text: str) -> list[str]:
    """Vulnerability IDs from `security/pip-audit-ignore.txt`, in file order.

    Blank lines and `#` comments are skipped, and a trailing `# ...`
    comment on an ID line is dropped -- the file's documented format puts
    the justification on its own line above the ID, but tolerating the
    inline form costs nothing and silently ignoring an ID because of it
    would be a hole in the gate.
    """
    ids: list[str] = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        match = _IGNORE_ID.match(candidate)
        if match:
            ids.append(match["id"])
    return ids


def reported_findings(report: dict) -> list[tuple[str, set[str]]]:
    """Each finding in a `pip-audit --format json` report, with its aliases.

    Returns `(label, every id that names this finding)` per finding, where
    the label is `"<package> <version>: <id>"` -- one vulnerability ID can
    be reported against several packages, so the ID alone would print the
    same line twice and name neither culprit.

    The alias set is deliberately NOT flattened across findings. The ignore
    file documents all three namespaces (PYSEC-, GHSA-, CVE-)
    interchangeably and pip-audit reports whichever its source used,
    listing the rest under `aliases` -- so suppression is a property of the
    FINDING, and one flat set of identifiers would report the other names
    of an already-suppressed finding as unsuppressed. An accepted
    vulnerability must not be able to fail the build under its own alias.
    """
    findings: list[tuple[str, set[str]]] = []
    for dependency in report.get("dependencies", []):
        for vuln in dependency.get("vulns") or []:
            primary = vuln.get("id") or ""
            names = {primary} | {alias for alias in (vuln.get("aliases") or []) if alias}
            label = f"{dependency.get('name', '?')} {dependency.get('version', '?')}: {primary}"
            findings.append((label, {name for name in names if name}))
    return findings


@dataclass
class PipAuditVerdict:
    """How a pip-audit report lands against the ignore file."""

    #: Findings no entry suppresses, as `"<package> <version>: <id>"` -- the
    #: original gate, unchanged.
    unsuppressed: list[str] = field(default_factory=list)
    #: Ignore entries that matched no finding -- the #4239 defect class.
    stale_ignores: list[str] = field(default_factory=list)
    #: Ignore entries that did suppress something, reported for visibility.
    live_ignores: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Pass only when nothing is unsuppressed and no entry is dead."""
        return not self.unsuppressed and not self.stale_ignores


def classify_pip_audit(report: dict, ignored: list[str]) -> PipAuditVerdict:
    """Split a pip-audit report into unsuppressed, live-ignored and stale.

    Deliberately derived from ONE unfiltered `pip-audit` run: passing
    `--ignore-vuln` and then asking "did anything go unused?" is not
    answerable from the filtered output, and running the scanner twice
    doubles the gate's cost to learn less.
    """
    findings = reported_findings(report)
    ignored_set = set(ignored)
    matched = {name for _, names in findings for name in names}
    return PipAuditVerdict(
        unsuppressed=sorted(label for label, names in findings if not names & ignored_set),
        stale_ignores=[entry for entry in ignored if entry not in matched],
        live_ignores=[entry for entry in ignored if entry in matched],
    )


def _stale_entry_message(entries: list[str], register: str, revisit: str) -> str:
    """The failure text, written once for both registers."""
    listed = "\n".join(f"  {entry}" for entry in entries)
    count = (
        "1 entry no longer suppresses"
        if len(entries) == 1
        else f"{len(entries)} entries no longer suppress"
    )
    return (
        f"\nSTALE SUPPRESSION: in {register}, {count} anything:\n"
        f"{listed}\n\n"
        "An entry that matches nothing still asserts 'examined, accepted' "
        "about a finding nobody can re-check, and it keeps the triage that "
        "justified it from ever being revisited. Either delete it -- "
        "recording WHY it went quiet, which is not always the condition the "
        "entry was watching (#4239: the advisory was withdrawn, not fixed) "
        f"-- or correct the entry.\n{revisit}"
    )


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a scanner with a fixed argv, capturing its combined output.

    No `# nosec` marker on this call, deliberately: `[tool.bandit]` excludes
    nothing under `scripts/`, but the CI step only scans `src/` and there is
    no bandit pre-commit hook, so a marker here would suppress nothing --
    which is the very class this module exists to fail the build on. The argv
    is fixed and `shell=False`; that is recorded here instead.
    """
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def check_npm_audit() -> int:
    """Run `audit-ci` and fail on findings OR on a dead allowlist entry."""
    if not allowlist_reporting_is_enabled(AUDIT_CI_CONFIG.read_text()):
        print(
            "web/audit-ci.jsonc sets `show-not-found: false`, which turns off "
            "the only report of an allowlist entry that matches nothing. "
            "Remove it: this gate has nothing else to read.",
            file=sys.stderr,
        )
        return 1

    result = _run(
        ["npx", "--no-install", "audit-ci", "--config", "audit-ci.jsonc"],
        cwd=WEB_DIR,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)

    if result.returncode != 0:
        return result.returncode

    if stale := stale_allowlist_entries(result.stdout + result.stderr):
        print(
            _stale_entry_message(
                stale,
                "web/audit-ci.jsonc's `allowlist`",
                "The per-entry reasoning and the REVISIT paragraph are in that "
                "file; update both in the same change.",
            ),
            file=sys.stderr,
        )
        return 1

    return 0


def check_pip_audit() -> int:
    """Audit Python dependencies and fail on findings OR on a dead ignore."""
    # `-m pip_audit` rather than the `pip-audit` console script: pip-audit
    # audits the environment of the interpreter it runs under, so running it
    # as a module of `sys.executable` makes the audited environment the same
    # one this gate was invoked from. The console script resolves off PATH and
    # can belong to a different venv entirely.
    result = _run(
        [sys.executable, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"],
        cwd=REPO_ROOT,
    )
    sys.stderr.write(result.stderr)

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fail closed and loudly: "could not read the report" must never be
        # reported as "no findings" (same rule as
        # scripts/agents/apply_codeql_adjudications.py).
        sys.stdout.write(result.stdout)
        print(
            f"pip-audit produced no parseable JSON report (exit {result.returncode}).",
            file=sys.stderr,
        )
        return 1

    ignored = parse_ignore_file(PIP_AUDIT_IGNORE.read_text())
    verdict = classify_pip_audit(report, ignored)

    for entry in verdict.live_ignores:
        print(f"suppressed by security/pip-audit-ignore.txt: {entry}")
    for entry in verdict.unsuppressed:
        print(f"UNSUPPRESSED vulnerability: {entry}", file=sys.stderr)

    if verdict.unsuppressed:
        print(
            "\npip-audit reports findings that no entry in "
            "security/pip-audit-ignore.txt accepts. Upgrade the dependency, "
            "or add the ID with its justification and today's date.",
            file=sys.stderr,
        )

    if verdict.stale_ignores:
        print(
            _stale_entry_message(
                verdict.stale_ignores,
                "security/pip-audit-ignore.txt",
                "The justification comment sits directly above each ID; "
                "delete it with the entry.",
            ),
            file=sys.stderr,
        )

    if verdict.ok:
        print(
            f"pip-audit: 0 unsuppressed findings, {len(ignored)} "
            "accepted entr" + ("y" if len(ignored) == 1 else "ies") + ", none stale."
        )
    return 0 if verdict.ok else 1


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the requested register's check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "register",
        choices=["npm-audit", "pip-audit"],
        help="which suppression register to audit for findings and dead entries",
    )
    args = parser.parse_args(argv)
    return check_npm_audit() if args.register == "npm-audit" else check_pip_audit()


if __name__ == "__main__":
    raise SystemExit(main())
