#!/usr/bin/env python3
"""Inverse-claims sweep: list the prose this diff may have made untrue (#4015).

`agents/runbooks/developer-runbook.md` §5 has required this sweep since #3744
and it gets skipped anyway, for a structural reason rather than a careless one.
The author is asked, at the moment they believe they are finished, to *recall*
which sentences anywhere in the tree their change falsified -- an open-ended
search with no starting point. The reviewer reads the diff cold and asks "what
does this contradict?", which is a much easier question, and that is why the
reviewer keeps winning: on 2026-08-22, five of eight PRs were blocked and four
of those five were blocked on nothing but stale prose (#3996, #4000, #4001,
#4005). Exactly one carried a real defect.

Every one of those had the same shape -- **prose elsewhere that names the thing
the diff changed** -- and that shape is mechanical. This script closes the
asymmetry by handing the author the reviewer's starting point:

  1. extract the identifiers the diff touches (commands, flags, endpoints,
     symbols, constants, file paths, issue refs) from its changed lines;
  2. search the *prose* of the rest of the tree -- markdown, comments,
     docstrings, workflow comments -- skipping the lines the diff already
     touched;
  3. keep only prose that makes a **claim** (negation, exclusivity,
     equivalence, deliberateness, world-state), because a passing mention is
     not something a change can falsify;
  4. rank, cap, and print the survivors as a checklist.

**It does not judge truth and it never fails on a hit.** Nothing it prints is
known to be wrong; the author confirms each line. A tool that cries wolf gets
ignored, and an ignored checklist is worse than no checklist, so the output is
capped at something a human will actually read and the suppressed count is
printed honestly rather than hidden.

Exit status is 0 whenever the sweep ran. Non-zero means the sweep itself could
not run (bad revision range, not a git tree) -- never "found something".

Usage:
    scripts/agents/inverse_claims_sweep.py                  # origin/<release>...HEAD
    scripts/agents/inverse_claims_sweep.py --base origin/v3.0.0 --head HEAD
    scripts/agents/inverse_claims_sweep.py --json
    scripts/agents/inverse_claims_sweep.py --markdown       # for a PR comment
"""

from __future__ import annotations

import argparse
import ast
import bisect
import json
import os
import re
import subprocess  # nosec B404 - git plumbing, fixed argv, no shell
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Tunables. Every default here was chosen by measuring the 2026-08-22 round
# (see tests/unit/test_inverse_claims_sweep.py and the PR body): the four known
# findings must survive, and the median diff must stay under a couple of dozen
# checklist rows. Widening any of these trades the second property for nothing:
# recall on the known four is already 4/4.
# --------------------------------------------------------------------------

#: A term that already appears in this many prose blocks is vocabulary, not an
#: identifier -- "config", "docs/README.md", "install". Searching for it
#: returns the tree, which is the cry-wolf failure mode.
#:
#: The budget is per *kind*, and deliberately lopsided. `nyxgpt ops status` is
#: named in 20-odd places precisely because it is a documented user surface, so
#: a flat cap tuned to kill `main.tf` also throws away the #3987 finding (the
#: measurement: a flat 8 loses docs/api.md entirely). A command or an endpoint
#: earns a wide budget because a hit on one is nearly always a real topic
#: match; a bare symbol or filename earns a narrow one because it is nearly
#: always a coincidence.
MAX_BLOCKS_PER_KIND: dict[str, int] = {
    "retired-path": 60,
    "command": 60,
    "endpoint": 60,
    "flag-value": 40,
    "class": 12,
    "function": 12,
    "constant": 12,
    "issue": 12,
    "path": 6,
    "flag": 6,
}

#: Scales every entry above; the CLI knob, so the trade-off stays inspectable.
DEFAULT_TERM_BUDGET = 1.0

#: Fact clusters printed. The rest are counted, not listed.
DEFAULT_MAX_ITEMS = 15

#: Places listed under one cluster before it collapses to a count. The density
#: number carries the message once the list is long; the point of a tall
#: cluster is the number, not the enumeration.
MAX_PLACES_PER_CLUSTER = 8

#: Lines of each block quoted in the checklist.
DEFAULT_PREVIEW_LINES = 4

#: Files bigger than this are data, not prose.
MAX_FILE_BYTES = 512 * 1024

EXCLUDED_DIR_PARTS = (
    ".git",
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".venv",
    # Superseded planning docs, kept deliberately as institutional history
    # (CLAUDE.md, owner reorganization 2026-07-31). A change cannot falsify
    # a document whose whole purpose is to record what was believed then.
    "archived_product_docs",
)

EXCLUDED_NAMES = (
    ".secrets.baseline",
    "package-lock.json",
    "poetry.lock",
    "yarn.lock",
)

# Suffix -> how to find prose in it.
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
PYTHON_SUFFIXES = (".py", ".pyi")
SLASH_COMMENT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss")
HASH_COMMENT_SUFFIXES = (
    ".yml",
    ".yaml",
    ".sh",
    ".bash",
    ".zsh",
    ".rb",
    ".tf",
    ".tfvars",
    ".toml",
    ".ini",
    ".cfg",
    ".tmpl",
    ".template",
    ".env",
)
HASH_COMMENT_NAMES = ("Dockerfile", "Makefile", ".gitignore", ".dockerignore")


# --------------------------------------------------------------------------
# Claim vocabulary
# --------------------------------------------------------------------------

#: What separates prose a change can *falsify* from prose that merely mentions
#: the thing. All four of the 2026-08-22 findings are claims of one of these
#: five kinds, and that is not a coincidence -- a sentence with no negation, no
#: exclusivity, no equivalence and no world-state is usually just a pointer,
#: and a pointer survives most changes.
CLAIM_MARKERS: dict[str, tuple[str, ...]] = {
    # "nyxGPT deliberately does not allocate one" (#3995)
    "negation": (
        r"\bnot\b",
        r"n't\b",
        r"\bnever\b",
        r"\bno longer\b",
        r"\bnothing\b",
        r"\bnone\b",
        r"\bneither\b",
        r"\bwithout\b",
        r"\brefus\w*",
        r"\breject\w*",
        r"\bdeclines?\b",
        r"\bcannot\b",
        r"\bunable\b",
        r"\bimpossible\b",
        r"\bskips?\b",
    ),
    # "so the three cannot disagree" (#3987)
    "equivalence": (
        r"\bthe same\b",
        r"\bidentical\b",
        r"\bverbatim\b",
        r"\bexactly\b",
        r"\bmirrors?\b",
        r"\bcannot disagree\b",
        r"\bin lockstep\b",
        r"\bas-is\b",
        r"\bone and the same\b",
    ),
    "exclusivity": (
        r"\bonly\b",
        r"\balways\b",
        r"\bevery\b",
        r"\bmust\b",
        r"\bguarantees?\b",
        r"\bensures?\b",
        r"\bthe one\b",
        r"\bsole\b",
        r"\bnothing else\b",
    ),
    # "the reason nyxGPT does not allocate one for you" (#3995)
    "deliberateness": (
        r"\bdeliberate\w*",
        r"\bintentional\w*",
        r"\bby design\b",
        r"\bon purpose\b",
        r"\bthe reason\b",
        r"\bwhich is why\b",
        r"\bso that\b",
        r"\brather than\b",
        r"\binstead of\b",
    ),
    # The expiry-dated world-state claims developer-runbook.md §5 forbids.
    "world-state": (
        r"\bcurrently\b",
        r"\bstill\b",
        r"\bnot yet\b",
        r"\bfor now\b",
        r"\bat present\b",
        r"\btoday\b",
        r"\bwill ship\b",
        r"\bplanned for\b",
        r"\bhas not shipped\b",
        r"\buntil\b",
        r"\bso far\b",
    ),
}

_CLAIM_RE: dict[str, re.Pattern[str]] = {
    kind: re.compile("|".join(pats), re.IGNORECASE) for kind, pats in CLAIM_MARKERS.items()
}


# --------------------------------------------------------------------------
# Term extraction
# --------------------------------------------------------------------------

# Weights are "how much does a hit on this term mean something". A retired file
# path or a full command string is a near-certain topic match; a bare `--force`
# or a two-segment path is mostly coincidence.
TERM_WEIGHTS: dict[str, float] = {
    "retired-path": 1.00,
    "command": 0.95,
    "endpoint": 0.90,
    "flag-value": 0.85,
    "class": 0.75,
    "function": 0.70,
    "constant": 0.60,
    "issue": 0.55,
    "path": 0.50,
    "flag": 0.30,
}

#: Kinds specific enough that a single match is worth a checklist row.
ANCHOR_KINDS = frozenset(
    {"retired-path", "command", "endpoint", "flag-value", "class", "function", "constant", "issue"}
)

_CODE_SPAN_RE = re.compile(r"`([^`\n]{2,90})`")
_COMMAND_RE = re.compile(
    r"^nyxgpt(?:\s+[a-z][a-z0-9-]*){1,3}(?:\s+--[a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9._:-]*)?)*$"
)
_FLAG_VALUE_RE = re.compile(r"(--[a-z][a-z0-9-]{1,30})[ =]([a-z][a-z0-9._:/-]{1,40})")
_FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]{2,30}\b")
_ENDPOINT_RE = re.compile(r"(?<![\w.])/(?:api/v\d+/)?[a-z][a-z0-9_-]*(?:/[a-z0-9_{}:-]+)+")
_PATH_RE = re.compile(
    r"(?<![\w/.-])[\w][\w.-]*(?:/[\w.-]+)*\."
    r"(?:py|pyi|ts|tsx|js|jsx|md|ya?ml|sh|rb|tf|toml|tmpl|ini|cfg|json|txt|rc)\b"
)
_SNAKE_RE = re.compile(r"(?<!\w)_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?!\w)")
_CAMEL_RE = re.compile(r"(?<!\w)[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+(?!\w)")
_CONST_RE = re.compile(r"(?<!\w)[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?!\w)")
_ISSUE_RE = re.compile(r"#\d{3,5}\b")

#: Absolute filesystem roots. `/opt/homebrew/bin/nyxgpt` and `/bin/bash` have
#: the shape of an HTTP route and none of the meaning; before this list they
#: were the top-ranked "endpoint" on two of the eight measured diffs.
NON_ENDPOINT_ROOTS = (
    "/bin",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/mnt",
    "/opt",
    "/private",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
    "/Users",
    "/Library",
    "/System",
    "/Applications",
    "/Volumes",
)

#: Tokens that pass the shape tests but carry no topic: pytest/mock/stdlib
#: keyword furniture and builtin exception names. Every one of these was
#: observed anchoring a checklist row on the 2026-08-22 diffs.
TERM_STOPWORDS = frozenset(
    {
        # dunder / test scaffolding
        "__init__",
        "__main__",
        "__name__",
        "__file__",
        "__pycache__",
        "set_up",
        "tear_down",
        "setup_method",
        "teardown_method",
        "monkeypatch",
        "tmp_path",
        "pytest_fixture",
        # keyword arguments of the stdlib and of unittest.mock
        "side_effect",
        "return_value",
        "capture_output",
        "check_output",
        "check_call",
        "store_true",
        "store_false",
        "exc_info",
        "exist_ok",
        "text_type",
        "read_text",
        "write_text",
        "encoding_errors",
        "no_qa",
        "type_ignore",
        "utf_8",
        "e_g",
        "i_e",
        # builtin and framework types
        "RuntimeError",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "OSError",
        "AttributeError",
        "AssertionError",
        "NotImplementedError",
        "FileNotFoundError",
        "CalledProcessError",
        "BaseModel",
        "HTTPException",
        "Exception",
        "TODO",
        "NOTE",
        "README",
        # flags every CLI in the world has
        "--help",
        "--version",
        "--verbose",
        "--quiet",
        "--force",
        "--dry-run",
        "--json",
        "--debug",
        "--yes",
        "--all",
        "--file",
        "--output",
        "--name",
        "--path",
    }
)

#: Paths whose mere mention proves nothing -- they are named by half the tree.
PATH_STOPWORDS = frozenset(
    {
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "docs/README.md",
        "agents/LEDGER.md",
        "config.ini",
        "package.json",
        "requirements.txt",
        "__init__.py",
    }
)


@dataclass(frozen=True)
class Term:
    """One identifier the diff touches, and why we think it is one."""

    text: str
    kind: str
    #: True when the term appears on both a removed and an added line -- the
    #: diff *changed* something about it rather than only mentioning it.
    modified: bool = False

    @property
    def weight(self) -> float:
        base = TERM_WEIGHTS.get(self.kind, 0.4)
        return base * (1.15 if self.modified else 1.0)

    @property
    def anchor(self) -> bool:
        """May a hit exist because of this term alone?

        `--host`, `cli.py`, `main.tf` are named by half the tree for reasons
        that have nothing to do with any one change; they are worth showing
        next to a real match and worthless as the reason for one. Measured on
        the 2026-08-22 round, 201 of 282 checklist rows on the largest diff
        rested on nothing but terms of that shape.
        """
        if self.kind in ANCHOR_KINDS:
            return True
        return self.kind == "path" and "/" in self.text


@dataclass
class Block:
    """A contiguous run of prose: a markdown paragraph or a comment run."""

    path: str
    start_line: int
    end_line: int
    text: str
    #: byte offset of this block inside the concatenated corpus
    offset: int = 0

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


@dataclass
class Hit:
    block: Block
    #: the claim sentence itself, which is what the author actually has to read
    sentence: str
    line: int
    terms: list[Term] = field(default_factory=list)
    claim_kinds: list[str] = field(default_factory=list)
    #: other claim sentences in the same block, collapsed into this row
    sibling_sentences: int = 0

    @property
    def score(self) -> float:
        term_score = max((t.weight for t in self.terms), default=0.0)
        # A second distinct term in the same sentence is real corroboration;
        # a fifth is not five times as interesting.
        corroboration = 1.0 + 0.15 * min(len(self.terms) - 1, 4)
        claim_score = 1.0 + 0.1 * min(len(self.claim_kinds) - 1, 3)
        return term_score * corroboration * claim_score


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path | None = None) -> Path:
    out = _git(["rev-parse", "--show-toplevel"], start or Path.cwd())
    return Path(out.strip())


def default_base(root: Path) -> str:
    """The active release branch, the way the rest of scripts/agents finds it."""
    env = os.environ.get("RELEASE_BRANCH")
    if env:
        return f"origin/{env}"
    for candidate in ("origin/v3.0.0", "origin/HEAD"):
        try:
            _git(["rev-parse", "--verify", "--quiet", candidate], root)
        except RuntimeError:
            continue
        return candidate
    return "origin/main"


# --------------------------------------------------------------------------
# Diff parsing
# --------------------------------------------------------------------------


@dataclass
class Diff:
    """What the range changed, in the two shapes the sweep needs."""

    added_lines: list[tuple[str, str]] = field(default_factory=list)
    removed_lines: list[tuple[str, str]] = field(default_factory=list)
    #: path -> post-image line numbers the diff wrote. A block overlapping one
    #: of these is prose the author already had in front of them.
    touched: dict[str, set[int]] = field(default_factory=dict)
    added_paths: set[str] = field(default_factory=set)
    removed_paths: set[str] = field(default_factory=set)

    @property
    def changed_paths(self) -> set[str]:
        return set(self.touched) | self.added_paths | self.removed_paths


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str) -> Diff:
    diff = Diff()
    path = ""
    old_path = ""
    new_line = 0
    is_new_file = False
    is_deleted_file = False

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            path = ""
            old_path = ""
            is_new_file = False
            is_deleted_file = False
            continue
        if raw.startswith("new file mode"):
            is_new_file = True
            continue
        if raw.startswith("deleted file mode"):
            is_deleted_file = True
            continue
        if raw.startswith("--- "):
            target = raw[4:].strip()
            old_path = "" if target == "/dev/null" else target[2:]
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                path = ""
                if old_path:
                    diff.removed_paths.add(old_path)
            else:
                path = target[2:]
                if is_new_file:
                    diff.added_paths.add(path)
                elif old_path and old_path != path:
                    diff.removed_paths.add(old_path)
                diff.touched.setdefault(path, set())
            continue
        m = _HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if not raw or raw[0] not in "+- ":
            continue
        body = raw[1:]
        if raw[0] == "+":
            if path:
                diff.added_lines.append((path, body))
                diff.touched.setdefault(path, set()).add(new_line)
                new_line += 1
        elif raw[0] == "-":
            src = old_path or path
            if src:
                diff.removed_lines.append((src, body))
            if is_deleted_file and old_path:
                diff.removed_paths.add(old_path)
        else:
            new_line += 1
    return diff


# --------------------------------------------------------------------------
# Term extraction
# --------------------------------------------------------------------------


def _terms_in_line(line: str) -> list[tuple[str, str]]:
    """(kind, text) pairs for one changed line."""
    found: list[tuple[str, str]] = []

    # Commands and flag phrases live in code spans in this repo's prose and in
    # string literals in its code; taking the span whole avoids swallowing the
    # sentence after it ("`nyxgpt ops status` again once the ollama service").
    for span in _CODE_SPAN_RE.findall(line):
        candidate = " ".join(span.split())
        if _COMMAND_RE.match(candidate):
            found.append(("command", candidate))

    for m in _FLAG_VALUE_RE.finditer(line):
        found.append(("flag-value", f"{m.group(1)} {m.group(2)}"))
    for m in _FLAG_RE.finditer(line):
        found.append(("flag", m.group(0)))
    for m in _ENDPOINT_RE.finditer(line):
        found.append(("endpoint", m.group(0)))
    for m in _PATH_RE.finditer(line):
        found.append(("path", m.group(0)))
    for m in _CONST_RE.finditer(line):
        found.append(("constant", m.group(0)))
    for m in _CAMEL_RE.finditer(line):
        found.append(("class", m.group(0)))
    for m in _SNAKE_RE.finditer(line):
        found.append(("function", m.group(0)))
    for m in _ISSUE_RE.finditer(line):
        found.append(("issue", m.group(0)))
    return found


def _plausible(kind: str, text: str) -> bool:
    if text in TERM_STOPWORDS:
        return False
    if kind == "path":
        if text in PATH_STOPWORDS or "/" not in text and text.count(".") < 1:
            return False
        return len(text) >= 6
    if kind in {"function", "class", "constant"}:
        return len(text) >= 6 and not text.startswith("test_")
    if kind == "command":
        return text.count(" ") >= 1
    if kind == "endpoint":
        if any(text == root or text.startswith(root + "/") for root in NON_ENDPOINT_ROOTS):
            return False
        return text.count("/") >= 2 or len(text) >= 10
    return True


def extract_terms(diff: Diff) -> list[Term]:
    """Identifiers the diff changes, most specific first."""
    added: dict[tuple[str, str], None] = {}
    removed: dict[tuple[str, str], None] = {}

    for _, line in diff.added_lines:
        for pair in _terms_in_line(line):
            added[pair] = None
    for _, line in diff.removed_lines:
        for pair in _terms_in_line(line):
            removed[pair] = None

    terms: dict[str, Term] = {}
    for kind, text in list(added) + list(removed):
        if not _plausible(kind, text):
            continue
        modified = (kind, text) in added and (kind, text) in removed
        existing = terms.get(text)
        candidate = Term(text=text, kind=kind, modified=modified)
        if existing is None or candidate.weight > existing.weight:
            terms[text] = candidate

    # A file the diff deletes or renames away is the strongest signal there is:
    # every sentence still naming it is describing something that is gone.
    for path in sorted(diff.removed_paths):
        stem = Path(path).stem
        terms[path] = Term(text=path, kind="retired-path")
        if len(stem) >= 6 and stem not in TERM_STOPWORDS:
            terms.setdefault(stem, Term(text=stem, kind="retired-path"))

    ordered = sorted(terms.values(), key=lambda t: (-t.weight, t.text))
    return ordered


# --------------------------------------------------------------------------
# Prose corpus
# --------------------------------------------------------------------------


def _is_tabular(block: Block) -> bool:
    """A markdown table row is an index entry, not a claim.

    `docs/api.md`'s endpoint table and `agents/CONTEXT_INDEX.md` are one
    paragraph each by the blank-line rule, so without this they arrive as a
    single enormous "sentence" that names dozens of identifiers and asserts
    nothing about any of them.
    """
    lines = [ln.strip() for ln in block.text.splitlines() if ln.strip()]
    if not lines:
        return True
    piped = sum(1 for ln in lines if ln.startswith("|"))
    return piped * 2 > len(lines)


def _is_excluded(path: str) -> bool:
    parts = Path(path).parts
    if any(part in EXCLUDED_DIR_PARTS for part in parts):
        return True
    return Path(path).name in EXCLUDED_NAMES


def _markdown_blocks(path: str, text: str) -> list[Block]:
    blocks: list[Block] = []
    buf: list[str] = []
    start = 0
    fenced = False
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            if buf:
                blocks.append(Block(path, start, idx - 1, "\n".join(buf)))
                buf = []
            continue
        if fenced:
            continue
        if not stripped:
            if buf:
                blocks.append(Block(path, start, idx - 1, "\n".join(buf)))
                buf = []
            continue
        if not buf:
            start = idx
        buf.append(line)
    if buf:
        blocks.append(Block(path, start, len(text.splitlines()), "\n".join(buf)))
    return blocks


def _prefixed_comment_blocks(path: str, text: str, prefix: str) -> list[Block]:
    """Runs of consecutive whole-line comments (`# ...` / `// ...`)."""
    blocks: list[Block] = []
    buf: list[str] = []
    start = 0
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(prefix):
            if not buf:
                start = idx
            body = stripped[len(prefix) :]
            # `#:` is this repo's attribute-doc convention; leaving the colon in
            # scatters " : " through every quoted sentence.
            buf.append(body[1:].strip() if body.startswith(":") else body.strip())
        elif buf:
            blocks.append(Block(path, start, idx - 1, "\n".join(buf)))
            buf = []
    if buf:
        blocks.append(Block(path, start, len(text.splitlines()), "\n".join(buf)))
    return blocks


def _python_blocks(path: str, text: str) -> list[Block]:
    """Comments and docstrings only -- parsed, not pattern-matched.

    The line-scanning version of this shipped first and was wrong: a bare
    `\"\"\"` closing a module-level string constant reads exactly like one
    opening a docstring, so the scanner swallowed the code after it and put
    `capture_output` at the top of a checklist. Prose extraction that
    misidentifies code as prose is the cry-wolf failure in miniature, so this
    uses `ast` for docstrings and `tokenize` for comments and accepts the
    parse cost.
    """
    blocks = _prefixed_comment_blocks(path, text, "#")
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return blocks
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        body = node.body[0]
        start = getattr(body, "lineno", 1)
        end = getattr(body, "end_lineno", start) or start
        blocks.append(Block(path, start, end, doc))
    return blocks


def _block_comment_blocks(path: str, text: str) -> list[Block]:
    blocks = _prefixed_comment_blocks(path, text, "//")
    for m in re.finditer(r"/\*(.*?)\*/", text, re.DOTALL):
        start = text.count("\n", 0, m.start()) + 1
        body = m.group(1)
        blocks.append(Block(path, start, start + body.count("\n"), body))
    return blocks


def blocks_for(path: str, text: str) -> list[Block]:
    name = Path(path).name
    suffix = Path(path).suffix
    if suffix in MARKDOWN_SUFFIXES:
        return _markdown_blocks(path, text)
    if suffix in PYTHON_SUFFIXES:
        return _python_blocks(path, text)
    if suffix in SLASH_COMMENT_SUFFIXES:
        return _block_comment_blocks(path, text)
    if suffix in HASH_COMMENT_SUFFIXES or name in HASH_COMMENT_NAMES:
        return _prefixed_comment_blocks(path, text, "#")
    # `.sh.tmpl`, `.rb.tmpl`, `foo.yml.j2` and friends.
    if any(part in HASH_COMMENT_SUFFIXES for part in (f".{p}" for p in name.split(".")[1:])):
        return _prefixed_comment_blocks(path, text, "#")
    return []


def _worktree_sources(root: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rel in _git(["ls-files", "-z"], root).split("\0"):
        if not rel or _is_excluded(rel):
            continue
        full = root / rel
        try:
            if full.is_symlink() or not full.is_file():
                # `src/nyxgpt/resources/docs/*.md` are tracked symlinks back to
                # `docs/` (#3621's packaging). Reading both copies doubles every
                # doc-based density and asks the author to fix one file twice.
                continue
            if full.stat().st_size > MAX_FILE_BYTES:
                continue
            out.append((rel, full.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _rev_sources(root: Path, rev: str) -> list[tuple[str, str]]:
    """Same listing, read out of a commit instead of the working tree.

    One `git cat-file --batch` for the whole tree: the obvious `git show
    rev:path` per file is thousands of processes and turns a two-second sweep
    into a two-minute one, which is the difference between a check that runs
    every time and one that gets skipped.
    """
    listing = _git(["ls-tree", "-r", "-z", rev], root).split("\0")
    wanted: list[tuple[str, str]] = []  # (oid, path)
    for entry in listing:
        if not entry or "\t" not in entry:
            continue
        meta, rel = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob" or _is_excluded(rel):
            continue
        if parts[0] == "120000":
            # A symlink blob holds its target path, not prose (see above).
            continue
        wanted.append((parts[2], rel))
    if not wanted:
        return []
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", "cat-file", "--batch"],
        cwd=str(root),
        input=("\n".join(oid for oid, _ in wanted) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git cat-file --batch failed: {proc.stderr.decode(errors='replace')}")
    blob = proc.stdout
    out: list[tuple[str, str]] = []
    pos = 0
    for _, rel in wanted:
        nl = blob.find(b"\n", pos)
        if nl < 0:
            break
        header = blob[pos:nl].split()
        size = int(header[-1]) if header and header[-1].isdigit() else 0
        body = blob[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
        if size > MAX_FILE_BYTES:
            continue
        try:
            out.append((rel, body.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return out


def collect_blocks(root: Path, diff: Diff, rev: str | None = None) -> list[Block]:
    """Every prose block in the tree the diff did not write."""
    sources = _rev_sources(root, rev) if rev else _worktree_sources(root)
    out: list[Block] = []
    for rel, text in sources:
        if rel in diff.added_paths:
            # Prose the author wrote in this diff cannot be prose the diff
            # falsified.
            continue
        touched = diff.touched.get(rel)
        for block in blocks_for(rel, text):
            if len(block.text.strip()) < 30 or _is_tabular(block):
                continue
            if touched and any(n in touched for n in range(block.start_line, block.end_line + 1)):
                continue
            out.append(block)
    return out


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


@dataclass
class Cluster:
    """Every place that asserts something about ONE thing the diff changed.

    This is the unit the output is built from, and the reason is the owner's
    2026-08-22 challenge to this issue. #3811's fix had to edit ten files
    because the same behavioural fact -- how a support ticket gets filed -- was
    written down in seven-plus places; the agent corrected nine of them and
    missed one, and that miss cost a review round. Listing those seven as seven
    independent rows says "go check seven unrelated things". Listing them as one
    cluster says "this one fact lives in seven files", which is a different and
    much more useful sentence: it is the measurement that decides whether the
    sweep is the whole fix or whether the tree needs a deduplication pass.
    """

    term: Term
    places: list[Hit] = field(default_factory=list)

    @property
    def density(self) -> int:
        """How many places assert this one fact."""
        return len(self.places)

    @property
    def files(self) -> list[str]:
        seen: dict[str, None] = {}
        for place in self.places:
            seen[place.block.path] = None
        return list(seen)

    @property
    def audiences(self) -> list[str]:
        seen: dict[str, None] = {}
        for path in self.files:
            seen[audience_of(path)] = None
        return sorted(seen)

    @property
    def score(self) -> float:
        return max((p.score for p in self.places), default=0.0)

    @property
    def near_duplicates(self) -> int:
        return _near_duplicates(self.places)

    @property
    def shape(self) -> str:
        """`copy-paste`, `restatement` or `unclear` -- a guess, for a human.

        The tool cannot tell legitimate audience-specific restatement from an
        accidental copy-paste, and it is not asked to: it is asked to surface
        the distinction so a person decides. Being visibly wrong here is more
        useful than being silent, so it always guesses and always says which
        evidence it used.
        """
        if self.density < 2:
            return "single"
        dup = self.near_duplicates
        if dup >= max(2, self.density // 2):
            return "copy-paste"
        if dup >= 2:
            return "mixed"
        if len(self.audiences) >= 2:
            return "restatement"
        return "unclear"

    @property
    def shape_note(self) -> str:
        if self.shape == "copy-paste":
            return (
                f"{self.near_duplicates} of {self.density} places use "
                f"near-identical wording across {len(self.files)} file(s) -- "
                "candidate for ONE canonical statement plus pointers, not N "
                "edits forever"
            )
        if self.shape == "mixed":
            return (
                f"mostly restatement for {len(self.audiences)} audience(s), but "
                f"{self.near_duplicates} places are near-identical -- worth "
                "deduplicating those"
            )
        if self.shape == "restatement":
            return (
                f"different wording for {len(self.audiences)} audience(s) "
                f"({', '.join(self.audiences)}) -- probably legitimate; "
                "update each"
            )
        if self.shape == "unclear":
            return f"{self.density} places, all in {self.audiences[0]} -- read them together"
        return ""


@dataclass
class SweepResult:
    clusters: list[Cluster]
    terms_extracted: int
    terms_searched: int
    terms_too_common: list[str]
    blocks_scanned: int
    blocks_matched: int
    base: str
    head: str

    @property
    def places(self) -> int:
        return sum(c.density for c in self.clusters)

    @property
    def densities(self) -> list[int]:
        return sorted((c.density for c in self.clusters), reverse=True)

    @property
    def median_density(self) -> float:
        d = sorted(c.density for c in self.clusters)
        if not d:
            return 0.0
        mid = len(d) // 2
        return float(d[mid]) if len(d) % 2 else (d[mid - 1] + d[mid]) / 2


#: Sentence boundaries. A markdown bullet or a `1.` item is its own claim even
#: without terminal punctuation, so a newline that starts a list item breaks too.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[ \t\n]+(?=[A-Z`\-*(\[])|\n(?=\s*(?:[-*+]|\d+\.)\s)")

#: Jaccard overlap at or above which two claim sentences are "the same
#: sentence, pasted" rather than "the same fact, retold".
COPY_PASTE_SIMILARITY = 0.5

_SIMILARITY_WORDS = (
    "a an and are as at be but by can for from has have if in is it its of on or "
    "so that the their then there these this to was were what when which will with "
    "not no you your it's"
)
_SIMILARITY_STOPWORDS = frozenset(_SIMILARITY_WORDS.split())

#: Who a file is written for. Two audiences describing the same behaviour is
#: the duplication CLAUDE.md's owner calls legitimate and permanent; two files
#: in one audience saying it the same way is the accidental kind.
AUDIENCE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("docs/", "user docs"),
    ("agents/", "agent process"),
    (".github/", "CI"),
    ("scripts/", "tooling"),
    ("product_management/", "product"),
    ("web/tests/", "test rationale"),
    ("tests/", "test rationale"),
    ("web/", "web code"),
    ("src/", "python code"),
    ("k8s/", "infra"),
    ("terraform/", "infra"),
    ("homebrew/", "infra"),
    ("docker/", "infra"),
    ("ops/", "infra"),
    ("security/", "infra"),
)


def audience_of(path: str) -> str:
    for prefix, name in AUDIENCE_BY_PREFIX:
        if path.startswith(prefix):
            return name
    return "entry docs" if path.endswith(".md") else "other"


def _shingle(sentence: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9_]+", sentence.lower())
    return frozenset(w for w in words if w not in _SIMILARITY_STOPWORDS and len(w) > 2)


def _near_duplicates(places: list[Hit]) -> int:
    """How many of these places have a near-identical partner among the rest.

    The first cut used the single most-similar pair, which called a 32-place
    cluster "copy-paste" because two of its thirty-two sentences happened to
    rhyme. Counting the places actually involved scales honestly with size.
    """
    sets = [_shingle(p.sentence) for p in places]
    paired = set()
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if not union:
                continue
            if len(sets[i] & sets[j]) / len(union) >= COPY_PASTE_SIMILARITY:
                paired.add(i)
                paired.add(j)
    return len(paired)


def _spread_by_file(places: list[Hit]) -> list[Hit]:
    """Best place per file first, then the seconds, then the thirds.

    A 36-place cluster truncated at 8 must show 8 *files*, not 8 sentences from
    the same doc: `docs/api.md`'s falsified line (#3987) sorted 16th on raw
    score and would have been below the fold, which is the same as not
    reporting it.
    """
    by_file: dict[str, list[Hit]] = {}
    for place in sorted(places, key=lambda p: (-p.score, p.block.path, p.line)):
        by_file.setdefault(place.block.path, []).append(place)
    ordered: list[Hit] = []
    rank = 0
    while len(ordered) < len(places):
        for group in by_file.values():
            if rank < len(group):
                ordered.append(group[rank])
        rank += 1
    return ordered


def _sentence_bounds(text: str) -> list[int]:
    edges = [0]
    for m in _SENTENCE_BREAK.finditer(text):
        edges.append(m.end())
    edges.append(len(text) + 1)
    return edges


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    """The sentence of `text` containing offset `pos`, as (start, end)."""
    edges = _sentence_bounds(text)
    i = bisect.bisect_right(edges, pos) - 1
    return edges[i], min(edges[i + 1], len(text))


def _claim_window(text: str, lo: int) -> str:
    """The anchor's sentence plus one neighbour on each side.

    A sentence is the right unit to *quote* and the wrong unit to *test*: in
    `test_macos_user_path_smoke.py` the symbol (`ops._native_service_version`)
    and the claim about it ("vendors `pyproject.toml` verbatim") are adjacent
    sentences of one docstring, and a strict per-sentence test misses the whole
    paragraph. One sentence of slack recovers that finding; more than one and
    the window is just the block again.
    """
    edges = _sentence_bounds(text)
    i = bisect.bisect_right(edges, lo) - 1
    start = edges[max(i - 1, 0)]
    end = min(edges[min(i + 2, len(edges) - 1)], len(text))
    return text[start:end]


def _cluster_rank(cluster: Cluster) -> float:
    """Specificity first, density second.

    Density is the message, but ranking on it alone puts `#3468` -- an issue
    number seven files happen to cite -- above `nyxgpt ops status`, the command
    the diff actually rewired. Weight the kind, then let density lift it.
    """
    return cluster.term.weight * (1.0 + 0.12 * min(cluster.density - 1, 8))


def _ceiling(kind: str, scale: float) -> float:
    return MAX_BLOCKS_PER_KIND.get(kind, 30) * scale


def _term_pattern(text: str) -> re.Pattern[str]:
    escaped = re.escape(text).replace(r"\ ", r"\s+")
    lead = r"(?<![\w-])" if text[:1].isalnum() or text[0] == "_" else ""
    trail = r"(?![\w-])" if text[-1:].isalnum() or text[-1] == "_" else ""
    return re.compile(lead + escaped + trail)


def sweep(
    root: Path,
    base: str,
    head: str,
    *,
    term_budget: float = DEFAULT_TERM_BUDGET,
    corpus_rev: str | None = None,
) -> SweepResult:
    diff_text = _git(["diff", "--unified=0", f"{base}...{head}"], root)
    if not diff_text.strip():
        diff_text = _git(["diff", "--unified=0", base, head], root)
    diff = parse_diff(diff_text)
    terms = extract_terms(diff)
    blocks = collect_blocks(root, diff, corpus_rev)

    corpus = "\n\x00\n".join(b.text for b in blocks)
    starts: list[int] = []
    cursor = 0
    for b in blocks:
        b.offset = cursor
        starts.append(cursor)
        cursor += len(b.text) + 3

    # (block index, sentence span) -> the terms found in that sentence
    per_sentence: dict[tuple[int, int, int], list[Term]] = {}
    searched = 0
    too_common: list[str] = []
    named_blocks: set[int] = set()
    corroborating: list[Term] = []
    for term in terms:
        # Cheap superset first: `str.count` is C-speed and kills the ~95% of
        # terms (test names, one-off locals) that appear nowhere in prose.
        if corpus.count(term.text) == 0:
            continue
        matches = [m.start() for m in _term_pattern(term.text).finditer(corpus)]
        if not matches:
            continue
        indexes = [bisect.bisect_right(starts, pos) - 1 for pos in matches]
        if len(set(indexes)) > _ceiling(term.kind, term_budget):
            # Named in more prose than any one change could be about: this is
            # the project's vocabulary ("config", "install", `main.tf`), and
            # expanding it returns the tree. Named in the summary rather than
            # dropped silently, because a term that trips this on a small diff
            # is itself worth a look.
            too_common.append(term.text)
            continue
        searched += 1
        named_blocks.update(indexes)
        if not term.anchor:
            # `--host` or `cli.py` enriches a sentence another term anchored;
            # on its own it is a coincidence, not a topic.
            corroborating.append(term)
            continue
        for pos, i in zip(matches, indexes, strict=True):
            local = pos - starts[i]
            span = _sentence_span(blocks[i].text, local)
            per_sentence.setdefault((i, span[0], span[1]), []).append(term)

    for term in corroborating:
        for m in _term_pattern(term.text).finditer(corpus):
            i = bisect.bisect_right(starts, m.start()) - 1
            local = m.start() - starts[i]
            for key, found in per_sentence.items():
                if key[0] == i and key[1] <= local < key[2] and term not in found:
                    found.append(term)

    places: list[Hit] = []
    for (i, lo, hi), found in per_sentence.items():
        block = blocks[i]
        sentence = block.text[lo:hi]
        window = _claim_window(block.text, lo)
        kinds = [k for k, rx in _CLAIM_RE.items() if rx.search(window)]
        if not kinds:
            # It names what you changed but asserts nothing about it. A pointer
            # survives most changes; only a claim can be falsified.
            continue
        found.sort(key=lambda t: (-t.weight, t.text))
        places.append(
            Hit(
                block=block,
                sentence=" ".join(sentence.split()),
                line=block.start_line + block.text.count("\n", 0, lo),
                terms=found,
                claim_kinds=kinds,
            )
        )

    # One place per prose block: three claim sentences in one paragraph are one
    # thing to read and one place to fix.
    best: dict[tuple[str, int], Hit] = {}
    for place in places:
        block_key = (place.block.path, place.block.start_line)
        current = best.get(block_key)
        if current is None or place.score > current.score:
            if current is not None:
                place.sibling_sentences = current.sibling_sentences + 1
            best[block_key] = place
        else:
            current.sibling_sentences += 1

    # Cluster by the strongest term in each place, so one fact is one row.
    grouped: dict[str, Cluster] = {}
    for place in best.values():
        head_term = place.terms[0]
        cluster = grouped.get(head_term.text)
        if cluster is None:
            cluster = Cluster(term=head_term)
            grouped[head_term.text] = cluster
        cluster.places.append(place)

    clusters = list(grouped.values())
    for cluster in clusters:
        cluster.places = _spread_by_file(cluster.places)
    # Density first: the biggest cluster is both the likeliest to hide a miss
    # and the one that says most about how duplicated this fact is.
    clusters.sort(key=lambda c: (-_cluster_rank(c), c.term.text))
    return SweepResult(
        clusters=clusters,
        terms_extracted=len(terms),
        terms_searched=searched,
        terms_too_common=too_common,
        blocks_scanned=len(blocks),
        blocks_matched=len(named_blocks),
        base=base,
        head=head,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_PREAMBLE = (
    "This is a CHECKLIST, not a gate. Nothing below is known to be wrong -- the\n"
    "sweep does not judge truth, it only finds prose that NAMES what you changed.\n"
    "Confirm each is still true once this merges, and fix the ones that are not,\n"
    "in THIS PR (developer-runbook.md 5, #3744)."
)

_DENSITY_NOTE = (
    "Each block below is ONE fact of yours, and every line under it is a place\n"
    "that asserts it. A tall block is not seven things to check -- it is one\n"
    "fact written down seven times, which is the #3811 shape: ten files edited,\n"
    "nine corrected, one missed, one review round lost (#4015 amendment)."
)


def _wrap(sentence: str, width: int, limit: int) -> list[str]:
    words = sentence.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
        if len(lines) == limit:
            return lines[:limit] + ["..."]
    if cur:
        lines.append(cur)
    return lines


def _stats_line(result: SweepResult) -> str:
    return (
        f"scanned {result.blocks_scanned} prose blocks for "
        f"{result.terms_searched} of {result.terms_extracted} identifiers "
        f"({len(result.terms_too_common)} are named too widely to be about any "
        f"one change); {result.blocks_matched} blocks named one, "
        f"{result.places} made a claim."
    )


def _density_line(result: SweepResult) -> str:
    d = result.densities
    if not d:
        return "duplication density: n/a (nothing to cluster)"
    return (
        f"duplication density: median {result.median_density:g}, worst {d[0]} "
        f"({sum(1 for n in d if n >= 5)} fact(s) asserted in 5+ places)"
    )


def render_text(result: SweepResult, *, max_items: int, preview_lines: int) -> str:
    out: list[str] = ["", f"Inverse-claims sweep  {result.base}...{result.head}", "=" * 72, ""]
    if not result.clusters:
        out.append(
            f"No prose in the tree makes a claim about the {result.terms_searched} "
            "identifier(s) this diff changes."
        )
        out += ["", _stats_line(result), ""]
        return "\n".join(out)

    out.append(
        f"{len(result.clusters)} fact(s) this diff changes are described elsewhere, "
        f"in {result.places} place(s)."
    )
    out += ["", _PREAMBLE, "", _DENSITY_NOTE, ""]

    for n, cluster in enumerate(result.clusters[:max_items], start=1):
        note = f"  <- {cluster.shape_note}" if cluster.shape_note else ""
        out.append(
            f"{n:3}. {cluster.term.text}   [{cluster.term.kind}]  "
            f"{cluster.density} place(s){note}"
        )
        for place in cluster.places[:MAX_PLACES_PER_CLUSTER]:
            more = f"  (+{place.sibling_sentences} more here)" if place.sibling_sentences else ""
            extra = [t.text for t in place.terms[1:4]]
            also = f"   also names: {', '.join(extra)}" if extra else ""
            out.append(f"     - {place.block.path}:{place.line}{more}{also}")
            for line in _wrap(place.sentence, 64, preview_lines):
                out.append(f"       | {line}")
        rest = cluster.places[MAX_PLACES_PER_CLUSTER:]
        if rest:
            # Named, never hidden: `docs/api.md` was the 22nd file of a 39-place
            # cluster on #3987, and a truncated list is the same as no report.
            out.append(f"     - also asserted, without the quote ({len(rest)}):")
            refs = [f"{p.block.path}:{p.line}" for p in rest]
            for line in _wrap(" ".join(refs), 64, 100):
                out.append(f"       {line}")
        out.append("")

    if len(result.clusters) > max_items:
        out += [
            f"     ... and {len(result.clusters) - max_items} lower-ranked fact(s), "
            "not listed. Re-run with --max-items 0 for all.",
            "",
        ]
    out += [_density_line(result), _stats_line(result), ""]
    return "\n".join(out)


def render_markdown(result: SweepResult, *, max_items: int) -> str:
    out: list[str] = ["### Inverse-claims sweep (#3744, mechanized by #4015)", ""]
    if not result.clusters:
        out.append(
            f"No prose in the tree makes a claim about the {result.terms_searched} "
            f"identifier(s) this diff changes ({result.blocks_scanned} prose blocks "
            "scanned)."
        )
        return "\n".join(out) + "\n"
    out.append(
        f"`scripts/agents/inverse_claims_sweep.py` grouped **{len(result.clusters)} "
        f"fact(s)** this diff changes, asserted in **{result.places} place(s)**. "
        f"{_density_line(result).capitalize()}."
    )
    out.append("")
    out.append(
        "The sweep does not judge truth. Each place was read and confirmed still "
        "true unless a commit here says otherwise."
    )
    out.append("")
    out.append("<details><summary>Checklist</summary>")
    out.append("")
    for n, cluster in enumerate(result.clusters[:max_items], start=1):
        note = f" — _{cluster.shape_note}_" if cluster.shape_note else ""
        out.append(f"{n}. **`{cluster.term.text}`** ({cluster.density} place(s)){note}")
        for place in cluster.places[:MAX_PLACES_PER_CLUSTER]:
            out.append(f"   - `{place.block.path}:{place.line}` — {place.sentence[:220]}")
        rest = cluster.places[MAX_PLACES_PER_CLUSTER:]
        if rest:
            refs = ", ".join(f"`{p.block.path}:{p.line}`" for p in rest)
            out.append(f"   - _also asserted in:_ {refs}")
    if len(result.clusters) > max_items:
        out += ["", f"_… and {len(result.clusters) - max_items} lower-ranked fact(s)._"]
    out += ["", "</details>"]
    return "\n".join(out) + "\n"


def render_json(result: SweepResult) -> str:
    return json.dumps(
        {
            "base": result.base,
            "head": result.head,
            "terms_extracted": result.terms_extracted,
            "terms_searched": result.terms_searched,
            "terms_too_common": result.terms_too_common,
            "blocks_scanned": result.blocks_scanned,
            "blocks_named_a_term": result.blocks_matched,
            "places": result.places,
            "median_density": result.median_density,
            "densities": result.densities,
            "clusters": [
                {
                    "term": c.term.text,
                    "kind": c.term.kind,
                    "density": c.density,
                    "shape": c.shape,
                    "shape_note": c.shape_note,
                    "audiences": c.audiences,
                    "files": c.files,
                    "places": [
                        {
                            "path": p.block.path,
                            "line": p.line,
                            "block_start_line": p.block.start_line,
                            "block_end_line": p.block.end_line,
                            "terms": [{"text": t.text, "kind": t.kind} for t in p.terms],
                            "claim_kinds": p.claim_kinds,
                            "sibling_sentences": p.sibling_sentences,
                            "score": round(p.score, 3),
                            "sentence": p.sentence,
                        }
                        for p in c.places
                    ],
                }
                for c in result.clusters
            ],
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List the prose elsewhere in the tree that describes what this diff "
            "changes, so the author can confirm it is still true. Advisory: "
            "always exits 0 when the sweep runs."
        )
    )
    parser.add_argument("--base", default=None, help="base ref (default: origin/<release branch>)")
    parser.add_argument("--head", default="HEAD", help="head ref (default: HEAD)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--markdown", action="store_true", help="PR-comment output")
    parser.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_ITEMS,
        help=f"fact clusters to print, 0 for all (default {DEFAULT_MAX_ITEMS})",
    )
    parser.add_argument(
        "--preview-lines",
        type=int,
        default=DEFAULT_PREVIEW_LINES,
        help=f"lines quoted per claim sentence (default {DEFAULT_PREVIEW_LINES})",
    )
    parser.add_argument(
        "--term-budget",
        type=float,
        default=DEFAULT_TERM_BUDGET,
        help=(
            "scale on MAX_BLOCKS_PER_KIND: an identifier named in more prose "
            "blocks than its kind's budget is vocabulary, not this change's "
            f"topic (default {DEFAULT_TERM_BUDGET})"
        ),
    )
    args = parser.parse_args(argv)

    try:
        root = repo_root()
        base = args.base or default_base(root)
        # A named head other than HEAD is swept against *that commit's* tree,
        # so the sweep can be run over history (its own acceptance test) without
        # checking anything out. Plain HEAD reads the working tree, so the sweep
        # sees work that is written but not yet committed.
        corpus_rev = None if args.head == "HEAD" else args.head
        result = sweep(
            root,
            base,
            args.head,
            term_budget=args.term_budget,
            corpus_rev=corpus_rev,
        )
    except RuntimeError as exc:
        print(f"[inverse-claims-sweep] could not run: {exc}", file=sys.stderr)
        return 2

    max_items = len(result.clusters) if args.max_items == 0 else args.max_items
    if args.json:
        print(render_json(result))
    elif args.markdown:
        print(render_markdown(result, max_items=max_items))
    else:
        print(render_text(result, max_items=max_items, preview_lines=args.preview_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
