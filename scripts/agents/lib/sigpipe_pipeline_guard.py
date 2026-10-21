#!/usr/bin/env python3
"""Guard against ``pipefail`` pipelines whose reader may leave before the end.

Motivating defect (#4192, review round 2): the macOS keg job's non-vacuity
assertion for #4043 was::

    set -euo pipefail
    brew services list | grep -q "$KEG" || {
      echo "::error::brew does not register a service for $KEG -- nothing here
            resolves to a versioned name, so this step is measuring nothing"
      exit 1
    }

``grep -q`` exits the instant it matches. That closes the read end, so the
producer's next write takes ``EPIPE``/``SIGPIPE`` and the producer exits
non-zero -- and under ``set -o pipefail`` the *pipeline* then reports that
failure even though the match succeeded. Run 38071858945 shows both halves in
sequence: ``nyxgpt-api@3.0.0rc`` listed, then
``Broken pipe @ rb_sys_fail_on_write - <STDOUT>``, then the step's own "this
step is measuring nothing" error. The same head had passed at 38070775763 --
whether the producer still has a write pending when the reader leaves is a
race, so the check's colour was not evidence about the head either way.

That is this issue's class exactly: a required gate whose red says nothing
about the commit. The failure mode is worse in a predicate. These two spell
the same hazard, and neither reports it::

    if nyxgpt ops port-forward --status | grep -q 'running'; then   # false NO
    loaded() { launchctl list | grep -q "$1"; }                     # false NO

A lost race there is silently read as "not matched": the assertion passes
vacuously, or the step skips the half it exists to prove.

**The reader is what decides, not the producer** -- and that is a correction,
recorded because the first draft of this guard got it wrong and shipped a
rewrite built on it. ``echo "$text" | grep -q`` looks immune, and a probe of a
1.2 MB payload agreed... because the payload was ONE long line, so ``grep``
had to read to the newline before it could match and therefore drained it.
The same byte count as 200k lines exits **141**: the builtin ``printf`` takes
``EPIPE`` like any other writer. Measured both ways in
``tests/unit/test_sigpipe_pipeline_guard.py``. So there is no safe producer,
only a safe reader.

**The spelling is not the class** -- and that is the second correction,
recorded because the first version of this guard knew only ``grep -q`` and
``head``, went green on 211 files, and then watched the identical defect
redden ``macos-brew-smoke``'s upgrade job on #4192's own PR (run 39679850952,
step "Build two candidates of the same formula from this checkout")::

    declared() {
      tar -xzOf "dist/$1/dist/nyxgpt-api-$2.tar.gz" "nyxgpt-api-$2/pyproject.toml" \
        | awk -F'"' '/^version =/{print $2; exit}'
    }
    RC1="$(declared rc1 3.0.0rc1)"

``awk``'s ``exit`` is ``grep -q`` by another name: the pyproject's version
line is near the top, so awk leaves with the rest of the member still to come,
``tar`` takes ``EPIPE`` (``Write error: Broken pipe`` / ``Error exit delayed
from previous errors``), and ``pipefail`` hands that to the command
substitution under ``set -e``. The step died *having read the right version*.
A guard that enumerates idioms rather than the behaviour -- "leaves before
EOF" -- is the same defect as the one it is checking for: a gate named for a
class, measuring one member of it. Every reader below is now enumerated from
the behaviour, and :data:`EARLY_READERS` is the list a reviewer should extend
rather than a new rule bolted alongside.

The supported forms, all of which drain or do not pipe at all::

    grep -q PATTERN <<<"$text"           # no pipe: nothing can be left unread
    grep -q PATTERN file                 # likewise
    producer | grep PATTERN >/dev/null   # plain grep reads to EOF; same status
    producer | sed -n '1,3p'             # same output as `head -3`, drains
    producer | awk '!v && /re/{v=$1} END{print v}'   # first match, drains

``grep`` without ``-q`` exits 0 on a match and 1 on none -- identical to
``-q`` -- and reads its whole input, so ``>/dev/null`` is a drop-in. ``sed -n
'1,Np'`` prints what ``head -N`` prints and also reads to EOF. ``awk`` holding
the first match in a variable and printing it from ``END`` prints what
``{print; exit}`` prints, having drained; an ``exit`` reached only from
``END`` is after EOF and is not flagged.

One boundary stated rather than left implied: an interpreter reader
(``producer | python3 script.py``) is opaque to any textual guard, so it is
not flagged. It is only a hazard if the script stops reading early -- nothing
in this tree does (``sys.stdin.read()`` throughout), and
``tests/unit/test_sigpipe_pipeline_guard.py`` pins that with its own scan
rather than leaving the gap unmeasured.

Two exemptions, each because the hazard genuinely is not there:

1. **Status explicitly discarded** (``|| true``, ``|| :``) -- the reader has
   already printed what the caller wanted; only the exit status was wrong, and
   it is thrown away. ``VAR="$(ls dist/*.whl | head -1)" || true`` extracts the
   right value in every outcome.
2. **``pipefail`` not in effect** -- without it the pipeline's status is the
   reader's, which is the answer. GitHub's default ``run:`` shell is
   ``bash -e {0}``, so this is only about blocks that opt in.

A pipeline this guard flags is a latent flake, not necessarily a live failure:
a producer that writes its whole output in one syscall and exits cannot take
``EPIPE``. The point is that nothing at the call site says which one it is --
``brew services list`` printed three lines and still lost the race.

Used by ``tests/unit/test_sigpipe_pipeline_guard.py`` and runnable as a CLI.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover - import shape depends on caller's sys.path
    from workflow_script_guard import iter_script_steps, workflow_files
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from workflow_script_guard import iter_script_steps, workflow_files

#: Readers that may leave before draining their input.
#:
#: ``grep`` only when it is told to stop early: ``-q``/``--quiet``/``--silent``
#: exit on the first match, ``-l``/``--files-with-matches`` on the first
#: matching file, ``-m N``/``--max-count=N`` after N matches. A plain ``grep``,
#: ``grep -c``, ``wc``, ``tail`` and ``sed`` all read to EOF and are fine.
_GREP_EARLY = re.compile(r"""(?x)
    (?:^|\s)
    (?:
        -[A-Za-z]*[qlL]            # short-option cluster containing q or l
      | --quiet | --silent
      | --files-with-matches
      | -m\s*\d+ | --max-count(?:=|\s)\s*\d+
    )
    (?:\s|$)
    """)

#: ``sed`` commands that stop reading: ``q`` quits, ``Q`` quits without
#: printing. Everything else in sed's language runs to EOF.
_SED_QUIT = re.compile(r"^[qQ]\b")

#: Addresses that may precede a sed command: line numbers, ``$``, ``first~step``
#: ranges, ``/re/`` and ``\cREc``, any of them negated with ``!``.
_SED_ADDRESS = re.compile(r"""(?x)
    ^(?:
        (?:[0-9]+(?:~[0-9]+)?|\$)            # 5, 1~3, $
      | /(?:\\.|[^/])*/[IM]*                 # /re/
      | \\(.)(?:\\.|(?!\1).)*\1[IM]*         # \cREc
    )
    (?:,\s*(?:
        (?:[0-9]+(?:~[0-9]+)?|\$|\+[0-9]+|~[0-9]+)
      | /(?:\\.|[^/])*/[IM]*
    ))?
    \s*!?\s*
    """)

#: ``sed`` options that take a separate argument, so the next word is not the
#: script. ``-e``/``--expression`` is handled on its own: its argument IS one.
_SED_ARG_OPTIONS = ("-f", "--file", "-l", "--line-length", "-i", "--in-place")

#: An ``awk`` ``END`` block, removed before looking for an early ``exit``.
#: ``END { ... exit 1 }`` runs after EOF: the input is already drained.
_AWK_END_BLOCK = re.compile(r"\bEND\s*\{(?:[^{}]|\{[^{}]*\})*\}")

#: ``set`` lines that turn ``pipefail`` on and off.
#:
#: The ``o`` may be anywhere in a short-option cluster -- ``set -euo pipefail``
#: is the spelling this tree actually uses, and a pattern insisting on a
#: standalone ``-o`` matches none of them (which is how the first draft of this
#: guard passed 208 files while the live defect sat in one of them).
_PIPEFAIL_ON = re.compile(r"\bset\s+-[A-Za-z]*o[A-Za-z]*\s+pipefail\b")
_PIPEFAIL_OFF = re.compile(r"\bset\s+\+[A-Za-z]*o[A-Za-z]*\s+pipefail\b")

#: Shell words that may precede the real producer on a line.
_LEADERS = re.compile(
    r"^(?:\s*(?:if|while|until|elif|then|else|do|!|\(|\{|&&|\|\||;)\s*)+",
)

#: ``NAME=`` / ``NAME=$(`` / ``"$(`` wrappers in front of a producer.
_ASSIGN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=)?\"?(?:\$\(|`)?\s*")

#: A pipeline whose status is deliberately thrown away.
_DISCARDED = re.compile(r"\|\|\s*(?:true\b|:\s*(?:\)|\}|$|;))")


@dataclass(frozen=True)
class Hazard:
    """One ``pipefail`` pipeline that can report its reader's early exit."""

    path: Path
    line: int
    producer: str
    reader: str
    text: str
    context: str = ""

    def format(self) -> str:
        where = f"{self.path}:{self.line}"
        if self.context:
            where += f" [{self.context}]"
        return f"{where}: `{self.producer}` | `{self.reader}` -- {self.text}"


def _shell_words(segment: str) -> list[str]:
    """``segment`` split into words, keeping quoted runs together.

    ``shlex`` is not usable here: these bodies carry ``${{ }}``, unbalanced
    quotes inside heredocs and ``$'...'``, and a parse error must not become a
    silent pass.
    """
    words: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in segment:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char.isspace():
            if current:
                words.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        words.append("".join(current))
    return words


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
        return word[1:-1]
    return word


def _sed_scripts(words: Sequence[str]) -> list[str]:
    """Every sed script in ``words``: each ``-e`` argument and the positional."""
    scripts: list[str] = []
    index = 0
    seen_positional = False
    while index < len(words):
        word = words[index]
        if word.startswith("-e") and word != "-e":
            scripts.append(_unquote(word[2:]))
        elif word == "-e" or word.startswith("--expression"):
            if "=" in word:
                scripts.append(_unquote(word.split("=", 1)[1]))
            elif index + 1 < len(words):
                index += 1
                scripts.append(_unquote(words[index]))
        elif word in _SED_ARG_OPTIONS:
            index += 1
        elif word.startswith("-") and word != "-":
            pass
        elif not seen_positional:
            seen_positional = True
            scripts.append(_unquote(word))
        index += 1
    return scripts


def _sed_quits(script: str) -> bool:
    """Whether ``script`` contains a ``q``/``Q`` command.

    Commands are read one at a time with their address stripped, so ``q`` as
    *data* -- ``s/q/x/``, ``/queued/d`` -- is not mistaken for the command.
    """
    rest = script.strip()
    while rest:
        rest = rest.lstrip("; \t\n{}")
        if not rest:
            break
        address = _SED_ADDRESS.match(rest)
        if address:
            rest = rest[address.end() :]
        if _SED_QUIT.match(rest):
            return True
        # Step over one command and whatever argument it carries, stopping at
        # the next unescaped `;` or newline outside a `s///`-style expression.
        if rest[:1] in ("s", "y") and len(rest) > 1:
            delim = rest[1]
            index, fields = 2, 0
            while index < len(rest) and fields < 2:
                if rest[index] == "\\":
                    index += 2
                    continue
                if rest[index] == delim:
                    fields += 1
                index += 1
            rest = rest[index:]
            continue
        cut = len(rest)
        for index, char in enumerate(rest):
            if char == "\\":
                continue
            if char in ";\n" and (index == 0 or rest[index - 1] != "\\"):
                cut = index
                break
        rest = rest[cut:]
    return False


def _awk_exits_early(words: Sequence[str]) -> bool:
    """Whether an ``awk`` program can ``exit`` before its input is drained."""
    program = " ".join(_unquote(w) for w in words[1:] if not w.startswith("-"))
    return bool(re.search(r"\bexit\b", _AWK_END_BLOCK.sub("", program)))


#: Every reader this guard knows can leave before EOF, keyed by the name a
#: hazard reports. Enumerated from the behaviour, not from idioms seen in this
#: tree: add to this list, do not add a parallel rule beside it.
EARLY_READERS: tuple[str, ...] = (
    "head",  # by definition: N lines, then gone
    "grep -q / -l / -m N",  # stops at the first match
    "sed ... q / Q",  # quits at an address
    "awk ... exit",  # outside END, so before EOF
    "read",  # one record, then gone
    "while/until read ... break",  # leaves the loop with input pending
)


def _reader_exits_early(segment: str) -> str | None:
    """Return the reader's name when ``segment`` may leave before EOF.

    One function for the whole of :data:`EARLY_READERS`, so a caller cannot
    consult half the class -- which is the defect that let ``awk ... exit``
    through a green guard (see the module docstring's second correction).
    """
    words = _shell_words(segment.strip())
    if not words:
        return None
    # `env A=b grep -q`, `command grep -q` and `sudo awk` still run the reader.
    skippable = re.compile(r"(?:env|command|time|sudo|builtin|exec|[A-Za-z_][A-Za-z0-9_]*=.*)")
    position = next(
        (i for i, w in enumerate(words) if not skippable.fullmatch(w)),
        0,
    )
    name = words[position]
    rest = words[position + 1 :]
    base = name.rsplit("/", 1)[-1].strip("\"'")
    if base == "busybox" and rest:  # `busybox awk ...` is awk
        base, rest = rest[0], rest[1:]
    label = segment.strip().split(";")[0].strip()

    if base == "head":
        return "head"
    if base == "read":
        return label
    if base in ("grep", "egrep", "fgrep", "zgrep", "ggrep") and _GREP_EARLY.search(
        " " + " ".join(rest)
    ):
        return label
    if base in ("sed", "gsed", "ssed") and any(_sed_quits(script) for script in _sed_scripts(rest)):
        return label
    if base in ("awk", "gawk", "mawk", "nawk") and _awk_exits_early([base, *rest]):
        return label
    return None


def _loop_reader_leaves_early(segment: str, following: Sequence[str]) -> str | None:
    """A ``while``/``until`` loop reading the pipe that ``break``s out of it.

    ``producer | while read x; do ...; break; done`` leaves with input
    pending, exactly as ``head -1`` does. A loop with no ``break`` drains, so
    this is measured by looking for one at the loop's own nesting depth --
    both of this tree's ``| while IFS= read`` loops are the draining kind, and
    saying so needs the lookahead rather than an assumption.
    """
    words = _shell_words(segment.strip())
    if not words or words[0].strip("({") not in ("while", "until"):
        return None
    if not re.search(r"(?:^|[\s;(])read\b", segment):
        return None
    depth = 0
    for text in [segment, *following]:
        for token in re.findall(r"\b(?:do|done|break)\b(?:\s+\d+)?", _strip_comment(text)):
            parts = token.split()
            if parts[0] == "do":
                depth += 1
            elif parts[0] == "done":
                depth -= 1
                if depth <= 0:
                    return None
            else:
                level = int(parts[1]) if len(parts) > 1 else 1
                if depth - level + 1 <= 1:
                    return segment.strip().split(";")[0].strip()
    return None


def _producer_word(segment: str) -> str:
    """The command whose writes would fail, as it is spelled in the source."""
    text = _LEADERS.sub("", segment).strip()
    # A function body opener (`name() {`) leaves the brace behind.
    text = re.sub(r"^[A-Za-z_][A-Za-z0-9_-]*\(\)\s*\{\s*", "", text).strip()
    text = _LEADERS.sub("", text).strip()
    text = _ASSIGN.sub("", text, count=1).strip()
    text = _LEADERS.sub("", text).strip()
    words = text.split()
    if not words:
        return ""
    word = words[0]
    for prefix in ("env", "command", "time", "sudo"):
        if word == prefix and len(words) > 1:
            word = words[1]
            break
    return word.strip("\"'`$({")


def split_pipeline(line: str) -> list[str]:
    """Split ``line`` on unquoted single ``|``, keeping ``||`` intact.

    Deliberately textual: a shell parser would be defeated by the ``${{ }}``
    substitution these bodies carry anyway, and the question here only needs
    pipeline boundaries.

    A command substitution opens a FRESH quoting context, and getting that
    wrong is a silent pass rather than a false alarm. The third live copy of
    #4192's own defect was invisible to the first version of this function::

        DECLARED="$(tar -xzOf "$tgz" "$member" | awk -F'"' '/^v/{print; exit}')"

    Counting quotes left-to-right makes the ``"`` before ``dist/`` *close* the
    outer one, so the ``|`` lands inside what the scan thinks is a quoted
    string and the line reports no pipeline at all. Bash reads it the other
    way: inside ``$( )`` the quoting starts over. The contexts are a stack
    here for the same reason, and ``'`` inside ``"..."`` stays literal.
    """
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    stack: list[str | None] = []
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            # Nothing is special inside single quotes, not even a backslash.
            current.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < len(line):
            current.append(line[index : index + 2])
            index += 2
            continue
        if line.startswith("$(", index) or char == "`":
            stack.append(quote)
            quote = None
            current.append(line[index : index + 2] if char == "$" else char)
            index += 2 if char == "$" else 1
            continue
        if char in (")", "`") and stack:
            quote = stack.pop()
            current.append(char)
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            current.append(char)
            index += 1
            continue
        if char == "'" and quote is None:
            quote = "'"
            current.append(char)
            index += 1
            continue
        if quote is None and char == "|":
            if index + 1 < len(line) and line[index + 1] == "|":
                current.append("||")
                index += 2
                continue
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def logical_lines(body: str) -> Iterator[tuple[int, str]]:
    """Yield ``(0-based offset of the first physical line, joined text)``.

    A pipeline may be written across several lines -- a trailing ``\\``, or a
    continuation line that *starts* with ``|``. Both halves have to be in hand
    at once or the producer is attributed to the wrong stage (or to nothing,
    which would silently pass).
    """
    physical = body.splitlines()
    index = 0
    while index < len(physical):
        start = index
        chunk = [physical[index]]
        while True:
            trailing_backslash = chunk[-1].rstrip().endswith("\\")
            next_line = physical[index + 1] if index + 1 < len(physical) else None
            continues = trailing_backslash or (
                next_line is not None
                and next_line.lstrip().startswith("|")
                and not next_line.lstrip().startswith("||")
            )
            if not continues or next_line is None:
                break
            index += 1
            chunk.append(physical[index])
        joined = " ".join(piece.rstrip().rstrip("\\").strip() for piece in chunk)
        yield start, joined
        index += 1


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, respecting quotes."""
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t;"):
            return line[:index]
    return line


def find_hazards(body: str, path: Path, context: str = "", base_line: int = 1) -> list[Hazard]:
    """Every flagged pipeline in one shell body."""
    hazards: list[Hazard] = []
    pipefail = False
    joined = list(logical_lines(body))
    for index, (offset, raw) in enumerate(joined):
        line = _strip_comment(raw)
        if _PIPEFAIL_ON.search(line):
            pipefail = True
        if _PIPEFAIL_OFF.search(line):
            pipefail = False
        if not pipefail or "|" not in line:
            continue
        if _DISCARDED.search(line):
            continue
        segments = split_pipeline(line)
        if len(segments) < 2:
            continue
        following = [text for _, text in joined[index + 1 :]]
        for stage in range(1, len(segments)):
            reader = _reader_exits_early(segments[stage]) or _loop_reader_leaves_early(
                segments[stage], following
            )
            if reader is None:
                continue
            # Reported for navigation only: there is no exemption for the
            # producer's identity. A builtin writing from memory takes EPIPE
            # like anything else once the payload outruns the pipe buffer --
            # see the module docstring's correction.
            producer = _producer_word(segments[stage - 1]) or "?"
            hazards.append(
                Hazard(
                    path=path,
                    line=base_line + offset,
                    producer=producer,
                    reader=reader,
                    text=raw.strip(),
                    context=context,
                )
            )
    return hazards


def scan_workflow(path: Path) -> list[Hazard]:
    """Flagged pipelines in every ``run:`` body of one workflow file."""
    hazards: list[Hazard] = []
    for step in iter_script_steps(path):
        if step.kind != "run":
            continue
        hazards.extend(
            find_hazards(
                step.script, path, context=f"{step.job} / {step.step}", base_line=step.line
            )
        )
    return hazards


def scan_shell(path: Path) -> list[Hazard]:
    """Flagged pipelines in one shell script."""
    return find_hazards(path.read_text(), path)


def scan(paths: Iterable[Path]) -> list[Hazard]:
    """Dispatch each path to the workflow or the shell scanner."""
    hazards: list[Hazard] = []
    for path in paths:
        if path.suffix in (".yml", ".yaml"):
            hazards.extend(scan_workflow(path))
        else:
            hazards.extend(scan_shell(path))
    return hazards


def shell_files(*roots: Path) -> list[Path]:
    """Every ``*.sh`` under ``roots``, sorted."""
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(p for p in root.rglob("*.sh") if p.is_file())
    return sorted(set(found))


def default_paths() -> list[Path]:
    """The gates and the scripts they run: what this guard is about."""
    return [*workflow_files(), *shell_files(Path("scripts"), Path("tests"))]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files to scan (default: .github/workflows plus scripts/ and tests/ shell scripts)",
    )
    args = parser.parse_args(argv)

    paths = args.paths or default_paths()
    hazards = scan(paths)
    if not hazards:
        print(
            f"OK: no pipefail pipeline in {len(paths)} file(s) can report its reader's early exit."
        )
        return 0

    print(
        f"{len(hazards)} pipefail pipeline(s) whose reader may leave before the "
        "end, so the pipeline can report the producer's EPIPE instead of the "
        "answer (#4192):",
        file=sys.stderr,
    )
    for hazard in hazards:
        print(f"  {hazard.format()}", file=sys.stderr)
    print(
        "\nUse a reader that drains, or no pipe at all:\n"
        '  grep -q PATTERN <<<"$text"            # no pipe\n'
        "  producer | grep PATTERN >/dev/null    # same status as -q, drains\n"
        "  producer | sed -n '1,3p'              # same output as head -3, drains\n"
        "  producer | awk '!v && /re/{v=$1} END{print v}'  # first match, drains\n"
        "or discard the status with `|| true` when only the reader's output is "
        "wanted.\n"
        "NOTE: `printf '%s\\n' \"$text\" | grep -q` is NOT a fix -- a builtin "
        "takes EPIPE too once the payload outruns the pipe buffer.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
