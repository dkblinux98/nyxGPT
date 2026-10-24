#!/usr/bin/env python3
"""Match open code-scanning alerts against the repo's adjudication register (#4222).

THE DEFECT. This project's decision that a code-scanning finding is acceptable
lived in one place only: GitHub's per-alert dismissal state. An alert's
identity includes its sink's LINE NUMBER, so an edit that merely moves a
dismissed sink re-raises the identical, already-accepted finding as a new open
alert. Nothing noticed until `scripts/release_ceremony.sh`'s code-scanning gate
refused to release -- which is how #143 (`cloud_deploy.py:3690`, dismissed
`won't fix`) came back as #144 at `:3924` and blocked v3.0.1, and how one
`ops doctor` flow was adjudicated four separate times (#21, #22, #23, #145) at
four sink lines.

THE RULE this module enforces:

    An adjudication is keyed to the FLOW -- rule id, the function enclosing
    the sink, and the functions the accepted flows start in -- never to a line
    number.

Line numbers move; functions get renamed deliberately. So the register in
`security/codeql-adjudications.yml` survives exactly the population of edits
that caused every re-raise above, and stops surviving when someone changes
what the code actually does, which is when a human should look again.

FAIL CLOSED, in both directions:

  * an open alert that matches no entry is REPORTED, never dismissed -- that
    is the case a human must adjudicate;
  * an entry only matches when the rule, the sink file, the sink function AND
    the flow's source function all agree. A new sensitive source reaching an
    already-accepted sink is a new finding, and gets reported. Dismissing it
    would be the trap this module exists to remove, rebuilt one level up.
  * a sink location whose enclosing function cannot be resolved -- the file is
    gone, or no longer parses -- matches nothing. "Could not tell" is not
    "accepted".

The alert payload names only the sink. Sources come from the analysis SARIF's
`codeFlows`, which is what `.github/workflows/code_scan_report.yml` already
prints for humans; `source_functions_by_sink` parses the same structure. An
alert whose flows are absent from the SARIF matches nothing, for the same
reason: an unverified source is not an accepted one.

WHAT COUNTS AS "THE SOURCE", because the obvious answer is wrong. The first
threadFlow step's *enclosing function* is not the taint source -- it is
whatever function the source expression happens to be written in. Measured on
this repo's own alerts: #144's two flows start at `cloud_screen.py:944` and
`:983`, which resolve to `_print_open_summary` and `_print_status_summary`,
the two CALLERS; and #145's starts inside `doctor`, which is also its sink, so
the key would carry no information at all. The step's `message.text` is what
names the symbol -- `ControlFlowNode for read_vnc_password()`,
`ControlFlowNode for _glitchtip_secrets_doctor_issues()` -- so that is the
key, with the enclosing function kept only as a fallback for a step CodeQL
labelled with an expression kind (`Fstring`, `BoolExpr`) instead of a name.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTER_PATH = REPO_ROOT / "security" / "codeql-adjudications.yml"

#: GitHub accepts only these three dismissal reasons. A typo in the register
#: would otherwise surface as an opaque 422 from the dismissal call.
VALID_REASONS = frozenset({"false positive", "won't fix", "used in tests"})

#: The severities `scripts/release_ceremony.sh`'s gate blocks a release on, so
#: the severities this has to keep clear. This filter decides only which
#: UNADJUDICATED alerts fail the run (`MatchReport.blocking_unadjudicated`):
#: an open medium the register does not cover is reported, not an error,
#: because nothing is gated on it. Matching and dismissal apply at EVERY
#: severity -- an entry in the register is a traced adjudication of that flow
#: whatever severity CodeQL assigned it, and holding the dismissal back would
#: leave the alert open against a reason the repository already records.
BLOCKING_SEVERITIES = frozenset({"critical", "high"})

#: `sink` for a sink at module level, where there is no enclosing function.
MODULE_SCOPE = "<module>"

#: GitHub's hard limit on `dismissed_comment`, learned by 422:
#: "Only 280 characters are allowed; 1043 were supplied." It is also the
#: explanation for the truncated dismissal comments already on this repo's
#: alerts -- #119's stops mid-word at "The alert's threat model - an attacke"
#: -- and the second reason the reasoning belongs in the repository: 280
#: characters cannot hold why a finding was accepted. So the alert gets
#: `headline` plus a pointer, and `comment` -- the full argument -- stays in
#: the register. Checked when the register LOADS, not at write time: a 422
#: mid-run would leave some alerts dismissed and the rest not.
MAX_DISMISSAL_COMMENT = 280

#: Where a reader of the alert goes for the full argument.
REGISTER_POINTER = "Reasoning + tests: security/codeql-adjudications.yml (#4222)."

#: CodeQL labels each flow step `ControlFlowNode for <what>`, sometimes with a
#: `[post]` marker and a trailing `[Attribute x]`. What is left is either a
#: named symbol -- which is the source this register keys on -- or an
#: expression kind (`Fstring`, `BoolExpr`, `List element`).
_STEP_LABEL_RE = re.compile(
    r"^\s*(?:\[post\]\s*)?(?:ControlFlowNode for\s*)?(?P<what>.*?)\s*(?:\[[^\]]*\])?\s*$"
)

#: A bare identifier, optionally called. `read_vnc_password()` -> the name;
#: `BoolExpr` is also a bare identifier, which is why the register names the
#: symbols it accepts rather than trusting whatever a step is labelled.
_SOURCE_SYMBOL_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\(\))?$")


class RegisterError(Exception):
    """The register is malformed. Always fatal: a register that cannot be read
    is indistinguishable from an empty one, and an empty one silently
    dismisses nothing while reporting every accepted finding as new."""


@dataclass(frozen=True)
class Adjudication:
    """One accepted finding, keyed to its flow rather than to a line."""

    rule: str
    path: str
    sink: str
    sources: frozenset[str]
    reason: str
    headline: str
    comment: str
    raised_as: tuple[int, ...] = ()
    enforced_by: tuple[str, ...] = ()

    def key(self) -> str:
        return f"{self.rule} {self.path}::{self.sink}"

    def matches(self, rule: str, path: str, sink: str, sources: Iterable[str]) -> bool:
        """Whether this entry adjudicates that flow.

        `sources` must be non-empty and wholly contained in the accepted set.
        Empty means the SARIF gave no flow for the alert, which is "could not
        tell" and never a match -- see the module docstring.
        """
        observed = {s for s in sources if s}
        if not observed:
            return False
        return (
            rule == self.rule
            and path == self.path
            and sink == self.sink
            and observed <= set(self.sources)
        )


@dataclass
class MatchReport:
    """What one pass over the open alerts decided."""

    adjudicated: list[tuple[dict[str, Any], Adjudication]] = field(default_factory=list)
    unadjudicated: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocking_unadjudicated(self) -> list[dict[str, Any]]:
        return [a for a in self.unadjudicated if alert_severity(a) in BLOCKING_SEVERITIES]


def load_register(path: Path | None = None) -> list[Adjudication]:
    """Parse and validate `security/codeql-adjudications.yml`.

    Every field is required except `raised_as`. `enforced_by` is required
    because an accepted finding whose premise nothing checks is a rule this
    project states and does not enforce (#4174) -- the register would then
    keep asserting "traced, not a defect" about code that had since changed.
    """
    path = path or REGISTER_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise RegisterError(f"no adjudication register at {path}") from exc
    except yaml.YAMLError as exc:
        raise RegisterError(f"{path} does not parse as YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegisterError(f"{path} must be a mapping with an `adjudications` key")

    entries = raw.get("adjudications")
    if entries is None:
        raise RegisterError(f"{path} has no `adjudications` key")
    if not isinstance(entries, list):
        raise RegisterError(f"{path}: `adjudications` must be a list")

    out: list[Adjudication] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{path} adjudications[{index}]"
        if not isinstance(entry, dict):
            raise RegisterError(f"{where} is not a mapping")
        for required in (
            "rule",
            "path",
            "sink",
            "sources",
            "reason",
            "headline",
            "comment",
            "enforced_by",
        ):
            if not entry.get(required):
                raise RegisterError(f"{where} is missing `{required}`")
        reason = str(entry["reason"])
        if reason not in VALID_REASONS:
            raise RegisterError(f"{where}: reason {reason!r} is not one of {sorted(VALID_REASONS)}")
        sources = entry["sources"]
        if not isinstance(sources, list):
            raise RegisterError(f"{where}: `sources` must be a list")
        enforced_by = entry["enforced_by"]
        if not isinstance(enforced_by, list):
            raise RegisterError(f"{where}: `enforced_by` must be a list")
        adjudication = Adjudication(
            rule=str(entry["rule"]),
            path=str(entry["path"]),
            sink=str(entry["sink"]),
            sources=frozenset(str(s) for s in sources),
            reason=reason,
            headline=" ".join(str(entry["headline"]).split()),
            comment=" ".join(str(entry["comment"]).split()),
            raised_as=tuple(int(n) for n in (entry.get("raised_as") or [])),
            enforced_by=tuple(str(t) for t in enforced_by),
        )
        written = len(dismissal_comment(adjudication))
        if written > MAX_DISMISSAL_COMMENT:
            raise RegisterError(
                f"{where}: the comment this would write to the alert is {written} characters "
                f"and GitHub allows {MAX_DISMISSAL_COMMENT}. Shorten `headline` by "
                f"{written - MAX_DISMISSAL_COMMENT} -- the full argument belongs in `comment`, "
                "which stays here and is not sent."
            )
        if adjudication.key() in seen:
            raise RegisterError(
                f"{where}: {adjudication.key()} is adjudicated twice -- merge the "
                "`sources` of the two entries into one"
            )
        seen.add(adjudication.key())
        out.append(adjudication)
    return out


def enclosing_function(path: Path, line: int) -> str | None:
    """Name of the innermost function containing `line`, or `MODULE_SCOPE`.

    None when the question cannot be answered -- the file is missing or no
    longer parses. Callers treat that as "no match", never as "accepted":
    the #3468 distinction between absence and failure-to-read, applied to a
    sink location.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None

    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        # Innermost wins: a nested helper's own name is the honest answer for a
        # sink inside it, and the outer function's would quietly widen what the
        # register accepts.
        if node.lineno <= line <= end and (best is None or node.lineno > best.lineno):
            best = node
    if best is not None:
        return best.name
    return MODULE_SCOPE


def step_symbol(message: str) -> str | None:
    """The symbol a flow step is labelled with, or None if it names no symbol.

    `"ControlFlowNode for read_vnc_password()"` -> `"read_vnc_password"`.
    `"ControlFlowNode for Fstring"` -> `"Fstring"`, which is an expression
    kind rather than a symbol, but telling the two apart is the register's job
    (it lists the symbols it accepts), not this function's.
    `"[post] ControlFlowNode for lines [List element]"` -> `"lines"`.
    """
    match = _STEP_LABEL_RE.match(message or "")
    if not match:
        return None
    symbol = _SOURCE_SYMBOL_RE.match(match.group("what"))
    return symbol.group("name") if symbol else None


def source_functions_by_sink(
    sarif: dict[str, Any], repo_root: Path | None = None
) -> dict[tuple[str, str, int], set[str]]:
    """Map `(ruleId, sink path, sink line)` -> the sources its flows start at.

    Reads EVERY `codeFlows` entry, not just the first: an alert stays open
    while any path lives, so an adjudication that covered only `codeFlows[0]`
    would accept a flow it never examined.

    Each flow contributes its FIRST threadFlow step, identified by the symbol
    CodeQL labelled it with and falling back to the step's enclosing function
    when the label names no symbol. See the module docstring for why the
    enclosing function alone is not enough.
    """
    repo_root = repo_root or REPO_ROOT
    out: dict[tuple[str, str, int], set[str]] = {}
    for run in sarif.get("runs") or []:
        for result in run.get("results") or []:
            rule = str(result.get("ruleId") or "")
            locations = result.get("locations") or []
            flows = result.get("codeFlows") or []
            if not rule or not locations or not flows:
                continue
            physical = (locations[0] or {}).get("physicalLocation") or {}
            sink_path = str((physical.get("artifactLocation") or {}).get("uri") or "")
            sink_line = int((physical.get("region") or {}).get("startLine") or 0)
            if not sink_path or not sink_line:
                continue
            bucket = out.setdefault((rule, sink_path, sink_line), set())
            for flow in flows:
                thread_flows = flow.get("threadFlows") or []
                if not thread_flows:
                    continue
                steps = (thread_flows[0] or {}).get("locations") or []
                if not steps:
                    continue
                first = (steps[0] or {}).get("location") or {}
                symbol = step_symbol(str((first.get("message") or {}).get("text") or ""))
                if symbol:
                    bucket.add(symbol)
                    continue
                region = first.get("physicalLocation") or {}
                src_path = str((region.get("artifactLocation") or {}).get("uri") or "")
                src_line = int((region.get("region") or {}).get("startLine") or 0)
                if not src_path or not src_line:
                    continue
                name = enclosing_function(repo_root / src_path, src_line)
                if name:
                    bucket.add(name)
    return out


def alert_severity(alert: dict[str, Any]) -> str:
    """The alert's severity, lowercased.

    `security_severity_level` is the one code-scanning's own UI and the
    release ceremony's gate both read; `rule.severity` is the fallback for a
    non-security rule, which carries no security severity at all.
    """
    rule = alert.get("rule") or {}
    return str(rule.get("security_severity_level") or rule.get("severity") or "").lower()


def alert_sink(alert: dict[str, Any]) -> tuple[str, str, int]:
    """`(ruleId, path, line)` of the alert's most recent instance."""
    rule = str((alert.get("rule") or {}).get("id") or "")
    location = ((alert.get("most_recent_instance") or {}).get("location")) or {}
    return rule, str(location.get("path") or ""), int(location.get("start_line") or 0)


def match_alerts(
    alerts: Iterable[dict[str, Any]],
    register: list[Adjudication],
    flow_sources: dict[tuple[str, str, int], set[str]],
    repo_root: Path | None = None,
) -> MatchReport:
    """Split `alerts` into those an entry adjudicates and those none does."""
    repo_root = repo_root or REPO_ROOT
    report = MatchReport()
    for alert in alerts:
        rule, path, line = alert_sink(alert)
        if not rule or not path or not line:
            report.unadjudicated.append(alert)
            continue
        sink = enclosing_function(repo_root / path, line)
        sources = flow_sources.get((rule, path, line), set())
        hit = next(
            (a for a in register if sink and a.matches(rule, path, sink, sources)),
            None,
        )
        if hit is None:
            report.unadjudicated.append(alert)
        else:
            report.adjudicated.append((alert, hit))
    return report


def dismissal_comment(adjudication: Adjudication) -> str:
    """The text written onto the alert: the headline, a pointer, the history.

    Deliberately NOT `comment`. GitHub allows 280 characters here, which is
    not enough to say why a finding was accepted -- so the alert carries the
    one-sentence verdict and the address of the full argument, and the
    argument itself stays in the register. The comment GitHub holds is a
    copy; the file is the original.

    `load_register` refuses an entry whose result exceeds
    `MAX_DISMISSAL_COMMENT`, so this never needs to truncate -- a silently
    cut-off reason is how #119's dismissal ends mid-word.
    """
    history = (
        f" Raised as {', '.join('#' + str(n) for n in adjudication.raised_as)}."
        if adjudication.raised_as
        else ""
    )
    return f"{adjudication.headline} {REGISTER_POINTER}{history}"
