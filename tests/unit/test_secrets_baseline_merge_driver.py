"""The `.secrets.baseline` merge driver (see scripts/git/merge-secrets-baseline.py).

The driver exists because `detect-secrets` rewrites `generated_at` on every
baseline update, so concurrent branches always conflict on a clock. These
tests pin the two properties that make it safe to run unattended on a
security-relevant file: it never LOSES a finding, and it refuses -- leaving
an ordinary conflict for a human -- whenever the two sides disagree about
anything that changes what the baseline means.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DRIVER = Path(__file__).resolve().parents[2] / "scripts" / "git" / "merge-secrets-baseline.py"


def _finding(name: str, line: int = 1) -> dict:
    return {
        "type": "Secret Keyword",
        "filename": name,
        "line_number": line,
        "hashed_secret": name * 3,
        "is_verified": False,
    }


def _baseline(**over) -> dict:
    doc = {
        "version": "1.5.0",
        "plugins_used": [{"name": "Base64HighEntropyString"}],
        "filters_used": [{"path": "detect_secrets.filters.allowlist.is_line_allowlisted"}],
        "results": {},
        "generated_at": "2026-01-01T00:00:00Z",
    }
    doc.update(over)
    return doc


def _run(tmp_path: Path, base: dict, ours: dict, theirs: dict) -> tuple[int, dict | None]:
    paths = {}
    for label, doc in (("O", base), ("A", ours), ("B", theirs)):
        p = tmp_path / f"{label}.json"
        p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        paths[label] = p
    rc = subprocess.call(
        [sys.executable, str(DRIVER), str(paths["O"]), str(paths["A"]), str(paths["B"])]
    )
    if rc != 0:
        return rc, None
    return rc, json.loads(paths["A"].read_text(encoding="utf-8"))


def test_the_clock_alone_merges_and_keeps_the_later_stamp(tmp_path: Path) -> None:
    """The whole reason the driver exists: identical findings, different clocks."""
    base = _baseline()
    ours = _baseline(generated_at="2026-08-22T19:10:40Z")
    theirs = _baseline(generated_at="2026-08-22T20:55:01Z")
    rc, merged = _run(tmp_path, base, ours, theirs)
    assert rc == 0
    assert merged is not None
    assert merged["generated_at"] == "2026-08-22T20:55:01Z"


def test_a_finding_from_either_side_survives(tmp_path: Path) -> None:
    """Losing a finding would let a real secret through on the next scan."""
    base = _baseline()
    ours = _baseline(results={"a.py": [_finding("a")]})
    theirs = _baseline(results={"b.py": [_finding("b")]})
    rc, merged = _run(tmp_path, base, ours, theirs)
    assert rc == 0
    assert merged is not None
    assert set(merged["results"]) == {"a.py", "b.py"}


def test_the_same_finding_on_both_sides_is_not_duplicated(tmp_path: Path) -> None:
    base = _baseline()
    both = {"a.py": [_finding("a")]}
    rc, merged = _run(tmp_path, base, _baseline(results=both), _baseline(results=both))
    assert rc == 0
    assert merged is not None
    assert merged["results"]["a.py"] == [_finding("a")]


def test_two_findings_in_the_same_file_are_unioned(tmp_path: Path) -> None:
    base = _baseline()
    ours = _baseline(results={"a.py": [_finding("a", 1)]})
    theirs = _baseline(results={"a.py": [_finding("a", 9)]})
    rc, merged = _run(tmp_path, base, ours, theirs)
    assert rc == 0
    assert merged is not None
    assert sorted(e["line_number"] for e in merged["results"]["a.py"]) == [1, 9]


def test_a_changed_plugin_set_refuses_rather_than_picking_a_side(tmp_path: Path) -> None:
    """A different plugin set changes what the baseline MEANS -- a human decides."""
    base = _baseline()
    theirs = _baseline(plugins_used=[{"name": "KeywordDetector"}])
    rc, merged = _run(tmp_path, base, _baseline(), theirs)
    assert rc == 1
    assert merged is None


def test_a_changed_filter_set_refuses(tmp_path: Path) -> None:
    base = _baseline()
    theirs = _baseline(filters_used=[{"path": "detect_secrets.filters.regex.should_exclude_file"}])
    rc, _ = _run(tmp_path, base, _baseline(), theirs)
    assert rc == 1


def test_a_changed_version_refuses(tmp_path: Path) -> None:
    rc, _ = _run(tmp_path, _baseline(), _baseline(), _baseline(version="1.6.0"))
    assert rc == 1


def test_an_unparseable_side_refuses_instead_of_writing_something(tmp_path: Path) -> None:
    o, a, b = (tmp_path / n for n in ("O.json", "A.json", "B.json"))
    o.write_text("{}", encoding="utf-8")
    a.write_text(json.dumps(_baseline()), encoding="utf-8")
    b.write_text("{not json", encoding="utf-8")
    rc = subprocess.call([sys.executable, str(DRIVER), str(o), str(a), str(b)])
    assert rc == 1
    assert json.loads(a.read_text(encoding="utf-8")) == _baseline()


def test_the_gitattributes_entry_points_at_this_driver() -> None:
    """The driver is inert unless .gitattributes maps the file to it."""
    root = Path(__file__).resolve().parents[2]
    attrs = (root / ".gitattributes").read_text(encoding="utf-8")
    assert ".secrets.baseline merge=secrets-baseline" in attrs
