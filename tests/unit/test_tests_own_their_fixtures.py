"""A test must not mutate state another reader can see (#4198).

THE DEFECT THIS EXISTS FOR. `tests/test_pr_head_guard_discriminates.sh` built
its fault injections in the **real checkout**: it rewrote three tracked files
in place and planted a tenth workflow under `.github/workflows/`, putting all
four back only from an `EXIT` trap. Two consequences, both observed:

* **A concurrent reader sees the injected tree.** A full `pytest` running in
  another process failed `test_context_index.py::test_committed_index_is_current`
  because the index generator globs `.github/workflows/*.yml` and saw the
  planted file. Benign that once -- but the planted file is a workflow whose
  entire purpose is to hand this repository's `workflow`-scoped PAT to a PR
  author, and for the length of the window the checkout really contains it.
* **`kill -9`, a job timeout or a runner eviction leaves it behind.** An `EXIT`
  trap runs on none of those, so the unsafe workflow stays on disk untracked
  and the three edits stay as local modifications, in a checkout nobody knows
  is dirty.

So "an `EXIT` trap restores it" is not a mitigation: it answers neither half.
The rule is that a test's fixture lives in a directory the test owns.

WHY THIS FILE EXISTS RATHER THAN A SENTENCE IN `docs/testing.md`. A rule this
project states and nothing enforces is its own defect class (#4174), and this
particular rule is invisible in review: `cp "$PRISTINE" "$WF"` looks like
tidying up, and nothing in the diff says `$WF` is a tracked file. So the rule
is a build failure.

WHAT IS FLAGGED. A write whose destination is inside the repository checkout,
in anything under `tests/`:

* shell -- a redirection, `cp`/`mv`/`rm`/`ln`/`mkdir`/`touch`/`tee`/`sed -i`
  destination, or a path argument handed to a heredoc program that writes,
  where the path resolves under the repo root through the suite's own
  variables (`$ROOT_DIR/...`, `$REPO_ROOT/...`, or a variable assigned from
  one) -- INCLUDING through a helper function, which is how two of the three
  tracked files in #4198 were patched: the call site is not a write and the
  body writes to `$1`, so only binding the one to the other finds it. Over
  the suite as it stood, the detector reports all 23 of its mutating lines;
  over the fixed suite, none;
* Python -- `write_text`/`write_bytes`/`unlink`/`mkdir`/`open(..., "w")`/
  `shutil.*`/`os.remove` and friends on a path derived from a
  `Path(__file__).resolve().parents[N]` constant.

Gitignored destinations are flagged too. `git status` staying clean is the
floor, not the rule: a planted `k8s/secret.yaml` is gitignored and is still a
file containing the string `api-key` that a `detect-secrets` hook, a parallel
test or the developer's next `python -m build` can read.

WHAT IS NOT FLAGGED, and is a known limit rather than an oversight: a write a
subprocess performs on its own (`python -m build <root>` leaves `build/` in
whatever root it is given), and a relative write after a `cd` into the root
that this file's one-line-at-a-time reading cannot connect. Both are detectable
only by running the suite; `tests/test_pr_head_guard_discriminates.sh`'s own
final half does exactly that -- it compares `git status --porcelain` across its
run -- which is the pattern to copy where it matters.

Both directions are checked, per #4167: `TestTheDetectorsAreNotVacuous` runs
each detector over the exact shapes this issue is about and requires a hit, and
over the legitimate `mktemp -d`/`tmp_path` shapes and requires silence. A
detector that has quietly stopped matching anything would otherwise read
exactly like a tree that has stopped offending.
"""

from __future__ import annotations

import ast
import re
import shlex
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"

#: Canonical stand-in for "the repository checkout" once a shell token's
#: variables have been resolved. A destination that expands to something
#: starting with this is a write into the shared tree.
_ROOT = "@ROOT@"

#: ...and for "somewhere this process owns", which is the correct answer.
_TMP = "@TMP@"

#: Unresolvable (a command substitution, a variable set outside the file, a
#: positional). Never an offender: the guard reports what it can read.
_UNKNOWN = "@?@"

#: `$NAME`, `${NAME}` and the positionals `$1`...`$9`.
_VARIABLE = re.compile(r"\$\{([A-Za-z_0-9][A-Za-z0-9_]*)[^}]*\}|\$([A-Za-z_0-9][A-Za-z0-9_]*)")

#: Shell variables holding the repo root. `ROOT_DIR` is the convention in
#: `tests/*.sh`; the other two appear in `scripts/`-shaped code.
_ROOT_VAR_NAMES = {"ROOT_DIR", "REPO_ROOT", "PROJECT_ROOT"}

#: The assignment that makes a variable the repo root in these suites:
#: `ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"`. Matched so a
#: suite that names its root variable something else is still understood.
_ROOT_ASSIGNMENT = re.compile(r"\$\(\s*cd\s+.*BASH_SOURCE.*&&\s*pwd\s*\)")

#: Commands whose LAST path argument is the destination.
_LAST_ARG_WRITERS = {"cp", "mv", "ln", "install", "rsync"}

#: Commands for which EVERY path argument is a destination.
_ALL_ARG_WRITERS = {"rm", "rmdir", "unlink", "touch", "mkdir", "tee", "truncate", "chmod", "chown"}

#: What makes a heredoc body a writer, for the fallback path -- a body this
#: file cannot parse (a shell or `jq` program rather than Python). The precise
#: path is `_written_argv_indices` below.
_HEREDOC_WRITES = re.compile(
    r"""open\([^)]*["']w["']|\.write_text\(|\.write_bytes\(|>\s*\$|shutil\.(copy|move|rmtree)"""
)

#: Python calls that write through the object they are called on.
_PATH_WRITE_METHODS = {
    "write_text",
    "write_bytes",
    "unlink",
    "mkdir",
    "touch",
    "rmdir",
    "rename",
    "replace",
    "symlink_to",
    "hardlink_to",
}

#: Python functions that write through an argument, mapped to the index of the
#: argument that is the DESTINATION. `shutil.copy(src, dst)` reading from the
#: checkout is fine; writing into it is not.
_ARG_WRITERS = {
    "remove": 0,
    "unlink": 0,
    "rmtree": 0,
    "mkdir": 0,
    "makedirs": 0,
    "rename": 1,
    "replace": 1,
    "copy": 1,
    "copy2": 1,
    "copyfile": 1,
    "copytree": 1,
    "move": 1,
    "symlink": 1,
}

#: The only receivers for which `_ARG_WRITERS` names a write, as a FULL dotted
#: name. Without this, every `text.replace(a, b)` in the suite reads as
#: `Path.replace`, a rename; matching the last component alone is not enough
#: either, because `sys.path.remove(...)` then reads as `os.path.remove`.
_ARG_WRITER_MODULES = {"os", "shutil", "os.path", "pathlib"}

#: Modes that make `open(path, mode)` a write.
_WRITE_MODES = ("w", "a", "x", "+")


# --------------------------------------------------------------------------
# the shell half
# --------------------------------------------------------------------------


def _strip_heredocs(text: str) -> tuple[list[tuple[int, str]], dict[int, str]]:
    """Split a shell script into command lines and the heredoc bodies they own.

    Returns `(lines, bodies)` where `lines` is `(lineno, line)` for every line
    that is a command (heredoc bodies removed -- they are data, and a Python
    body is full of quotes bash never parses), and `bodies` maps the line
    number of the command that opened a heredoc to that heredoc's body.
    """
    lines: list[tuple[int, str]] = []
    bodies: dict[int, str] = {}
    delimiter: str | None = None
    owner = 0
    collected: list[str] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        if delimiter is not None:
            if line.strip() == delimiter:
                bodies[owner] = "\n".join(collected)
                delimiter, collected = None, []
            else:
                collected.append(line)
            continue
        lines.append((lineno, line))
        opener = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        if opener:
            delimiter = opener.group(2)
            owner = lineno
    if delimiter is not None:  # unterminated -- keep what we have
        bodies[owner] = "\n".join(collected)
    return lines, bodies


def _expand(token: str, variables: dict[str, str]) -> str:
    """Resolve `$VAR`/`${VAR}` in a shell token to the sentinels above."""
    if "$(" in token or "`" in token:
        # A command substitution: resolvable only if the whole token is one we
        # recognise as a temp directory.
        return _TMP if "mktemp" in token else _UNKNOWN

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name in _ROOT_VAR_NAMES:
            return _ROOT
        return variables.get(name, _UNKNOWN)

    # Digits on purpose: `$1`/`$2` are how a helper function refers to the
    # path its caller handed it, and that indirection is how two of the three
    # tracked files in #4198 were patched.
    return re.sub(_VARIABLE, replace, token)


#: An assignment, including the several on one `local a="$1" b="$2"` line.
#: `(?:^|(?<=\s))` with the `-` lookahead excluded keeps `--flag=value` out.
_ASSIGNMENT = re.compile(r"""(?:^|(?<=\s))(?!-)([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|'[^']*'|\S*)""")


def _shell_variables(
    lines: list[tuple[int, str]], seed: dict[str, str] | None = None
) -> dict[str, str]:
    """Map each simply-assigned variable in a suite to its expansion.

    `seed` is the scope the lines are read in. It is how a helper function's
    body is resolved at a particular call site: with `$1` bound to the
    caller's argument, `local file="$1"` makes `file` the caller's path, which
    is the indirection `_patch_last_ref "$WF"` uses.
    """
    variables: dict[str, str] = dict(seed or {})
    for _, line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for name, raw in _ASSIGNMENT.findall(line):
            if name in _ROOT_VAR_NAMES or _ROOT_ASSIGNMENT.search(raw):
                variables[name] = _ROOT
                continue
            if raw[:1] in {'"', "'"} and raw[-1:] == raw[:1]:
                raw = raw[1:-1]
            variables[name] = _expand(raw, variables)
    return variables


def _is_option(token: str) -> bool:
    return token.startswith("-") and token != "-"


def _destinations(tokens: list[str]) -> list[str]:
    """The tokens a shell command line writes TO."""
    dests: list[str] = []
    words = [t for t in tokens if t not in {"&&", "||", ";", "|", "(", ")", "{", "}", "!"}]

    for index, token in enumerate(tokens):
        if token in {">", ">>", "1>", "2>", "&>", ">|"} and index + 1 < len(tokens):
            dests.append(tokens[index + 1])

    command = next((w for w in words if "=" not in w.split("/")[0] or w.startswith("/")), None)
    if command is None:
        return dests
    name = Path(command).name
    args = [w for w in words[words.index(command) + 1 :] if not _is_option(w)]

    if name in _LAST_ARG_WRITERS and args:
        dests.append(args[-1])
    elif name in _ALL_ARG_WRITERS:
        dests.extend(args)
    elif name == "sed" and any(t.startswith("-i") for t in tokens):
        dests.extend(args[1:] if len(args) > 1 else [])
    return dests


def _tokenize(line: str) -> list[str] | None:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _written_argv_indices(body: str) -> set[int] | None:
    """Which `sys.argv[N]` a heredoc Python program WRITES to.

    This is the form four of the #4198 injections took -- the destination is
    not on the command line at all:

        python3 - "$WF" '${{ github.head_ref }}' <<'PY'
        path, replacement = sys.argv[1], sys.argv[2]
        open(path, "w").write(...)
        PY

    Reading the body is what keeps the rule precise in BOTH directions. Two
    suites hand such a program a tracked file to READ and a `$TMP` path to
    write (`tests/test_admin_set_fields.sh`, `tests/test_ensure_pr_exists.sh`
    both do `python3 - "$SCRIPT" "$INJECTED"`, reverting a shipped script into
    a temp copy); flagging every repo-rooted argument would condemn them, and
    a guard that condemns the correct shape is one the next person switches
    off. Returns `None` when the body is not parseable Python, which sends the
    caller to the coarse fallback.
    """
    try:
        with warnings.catch_warnings():
            # A *shell* heredoc body can be syntactically valid Python and full
            # of `\$`, which `ast.parse` reports as an invalid escape. That is
            # a fact about the bash we are reading, not a defect to surface.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(body)
    except SyntaxError:
        return None

    # `path = sys.argv[1]`, `a, b = sys.argv[1], sys.argv[2]`,
    # `src, out = sys.argv[1:3]`, `path = Path(sys.argv[1])`.
    argv_names: dict[str, int] = {}

    def argv_index(node: ast.AST) -> int | None:
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Subscript)
                and isinstance(sub.value, ast.Attribute)
                and sub.value.attr == "argv"
                and isinstance(sub.slice, ast.Constant)
                and isinstance(sub.slice.value, int)
            ):
                return sub.slice.value
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = node.targets[0]
        value = node.value
        if isinstance(targets, ast.Tuple) and isinstance(value, ast.Tuple):
            for target, element in zip(targets.elts, value.elts, strict=True):
                index = argv_index(element)
                if isinstance(target, ast.Name) and index is not None:
                    argv_names[target.id] = index
        elif isinstance(targets, ast.Tuple) and isinstance(value, ast.Subscript):
            # `src, out = sys.argv[1:3]` -- consecutive from the slice start.
            lower = value.slice.lower if isinstance(value.slice, ast.Slice) else None
            start = lower.value if isinstance(lower, ast.Constant) else None
            if isinstance(start, int):
                for offset, target in enumerate(targets.elts):
                    if isinstance(target, ast.Name):
                        argv_names[target.id] = start + offset
        elif isinstance(targets, ast.Name):
            index = argv_index(value)
            if index is not None:
                argv_names[targets.id] = index

    def written_index(node: ast.AST) -> int | None:
        index = argv_index(node)
        if index is not None:
            return index
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in argv_names:
                return argv_names[sub.id]
        return None

    written: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target: ast.AST | None = None
        if isinstance(func, ast.Name) and func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else None
            if isinstance(mode, ast.Constant) and any(m in str(mode.value) for m in _WRITE_MODES):
                target = node.args[0]
        elif isinstance(func, ast.Attribute) and func.attr in _PATH_WRITE_METHODS:
            target = func.value
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            mode = node.args[0] if node.args else None
            if isinstance(mode, ast.Constant) and any(m in str(mode.value) for m in _WRITE_MODES):
                target = func.value
        if target is not None:
            index = written_index(target)
            if index is not None:
                written.add(index)
    return written


def _positional_arguments(tokens: list[str]) -> list[str]:
    """The `sys.argv` a heredoc program on this command line would see.

    `python3 - a b` -> argv[0] is `-`, so a is argv[1]; `python3 x.py a` the
    same. Options and redirections are dropped.
    """
    words: list[str] = []
    for token in tokens:
        if token in {"&&", "||", ";", "|", "(", ")", ">", ">>", "<", "<<"}:
            break
        words.append(token)
    interpreters = [i for i, w in enumerate(words) if Path(w).name.startswith(("python", "bash"))]
    if not interpreters:
        return []
    rest = [w for w in words[interpreters[0] + 1 :] if not _is_option(w)]
    return rest  # rest[0] is the script (`-`), so argv[N] is rest[N]


def _shell_functions(lines: list[tuple[int, str]]) -> dict[str, list[tuple[int, str]]]:
    """`name -> body lines` for each function the script defines.

    Needed because a suite can route the write through a helper, which is how
    the one this issue is about patched two of its three tracked files:

        _patch_last_ref() { python3 - "$1" "$2" <<'PY' ... }
        _patch_last_ref "$WF" '${{ github.head_ref }}'

    Nothing on either line is a repo-rooted destination by itself -- `$1` is
    unresolvable in the body, and the call site is not a write. Binding the
    call's arguments to the body's positionals is what connects them.
    """
    functions: dict[str, list[tuple[int, str]]] = {}
    name: str | None = None
    body: list[tuple[int, str]] = []
    for lineno, line in lines:
        if name is None:
            opener = re.match(r"\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{\s*$", line)
            if opener:
                name, body = opener.group(1), []
            continue
        if line.rstrip() == "}":
            functions[name] = body
            name = None
            continue
        body.append((lineno, line))
    return functions


def _line_destinations(tokens: list[str], body: str | None, variables: dict[str, str]) -> list[str]:
    """Every token one command line writes to, heredoc destinations included."""
    dests = _destinations(tokens)
    if not body:
        return dests
    written = _written_argv_indices(body)
    if written is None:
        # Not Python: fall back to "a writing body means every repo-rooted
        # argument is suspect", unless the line also hands it a path it owns,
        # which is then the likely destination.
        if _HEREDOC_WRITES.search(body) and not any(
            _expand(token, variables).startswith(_TMP) for token in tokens
        ):
            dests.extend(tokens)
        return dests
    argv = _positional_arguments(tokens)
    dests.extend(argv[index] for index in sorted(written) if index < len(argv))
    return dests


def shell_offenders(text: str) -> list[str]:
    """Writes into the repo checkout made by a shell script's own commands."""
    lines, bodies = _strip_heredocs(text)
    variables = _shell_variables(lines)
    functions = _shell_functions(lines)
    offenders: list[str] = []

    def writes_into_the_checkout(dests: list[str], bindings: dict[str, str]) -> bool:
        scope = {**variables, **bindings}
        return any(_expand(dest, scope).startswith(_ROOT + "/") for dest in dests)

    for lineno, line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = _tokenize(line)
        if tokens is None:
            continue

        if writes_into_the_checkout(_line_destinations(tokens, bodies.get(lineno), variables), {}):
            offenders.append(f"{lineno}: {stripped}")
            continue

        # A call to one of this script's own functions: bind the arguments to
        # the body's positionals and ask the same question of the body. One
        # level deep, which is all these suites use and all that can be read
        # without tracking the call graph.
        words = [t for t in tokens if t not in {"&&", "||", ";", "|", "(", ")"}]
        if not words or words[0] not in functions or "{" in words:
            continue  # `name() {` is the definition, not a call
        arguments = words[1:]
        bindings = {
            str(index + 1): _expand(argument, variables) for index, argument in enumerate(arguments)
        }
        # Re-resolve the body's locals with the arguments bound, so
        # `local file="$1"` carries the caller's path.
        bindings = _shell_variables(functions[words[0]], {**variables, **bindings})
        for body_lineno, body_line in functions[words[0]]:
            body_tokens = _tokenize(body_line)
            if body_tokens is None:
                continue
            dests = _line_destinations(body_tokens, bodies.get(body_lineno), variables)
            if writes_into_the_checkout(dests, bindings):
                offenders.append(f"{lineno}: {stripped}")
                break
    return offenders


# --------------------------------------------------------------------------
# the Python half
# --------------------------------------------------------------------------


def _is_repo_root_expr(node: ast.AST) -> bool:
    """True for `Path(__file__).resolve().parents[N]` and `.parent` chains."""
    source = ast.dump(node)
    return "__file__" in source and ("parents" in source or "parent" in source)


def _mentions_tmp(node: ast.AST) -> bool:
    source = ast.dump(node)
    return any(token in source for token in ("tmp_path", "tmpdir", "mkdtemp", "TemporaryDirectory"))


def _is_path_expr(node: ast.AST) -> bool:
    """True if this expression PRODUCES a path, rather than merely mentioning one.

    The distinction matters more than it looks. `text = (ROOT / "x").read_text()`
    mentions a repo path and yields a *string*, and treating the result as a
    path made `text.replace(a, b)` -- `str.replace`, in half a dozen suites --
    read as `Path.replace`, a rename. A name is only in the checkout if it was
    built by path arithmetic.
    """
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return True
    if isinstance(node, ast.Attribute):
        return node.attr in {"parent", "parents"}
    if isinstance(node, ast.Subscript):
        return _is_path_expr(node.value)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "Path"
        if isinstance(func, ast.Attribute):
            return func.attr in {
                "joinpath",
                "resolve",
                "absolute",
                "expanduser",
                "with_name",
                "with_suffix",
            }
    return False


def _derives_from(node: ast.AST, names: set[str]) -> bool:
    if _mentions_tmp(node):
        return False
    return any(isinstance(sub, ast.Name) and sub.id in names for sub in ast.walk(node))


def _dotted_name(node: ast.AST) -> str:
    """`os.path` for `ast.Attribute(Name("os"), "path")`, "" for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _write_target(node: ast.Call, names: set[str]) -> bool:
    """Does this call write to a path inside the checkout?"""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in _PATH_WRITE_METHODS:
            return _derives_from(func.value, names)
        if func.attr == "open":
            mode = node.args[0] if node.args else None
            return (
                isinstance(mode, ast.Constant)
                and any(m in str(mode.value) for m in _WRITE_MODES)
                and _derives_from(func.value, names)
            )
        if func.attr in _ARG_WRITERS:
            if _dotted_name(func.value) not in _ARG_WRITER_MODULES:
                return False
            index = _ARG_WRITERS[func.attr]
            return len(node.args) > index and _derives_from(node.args[index], names)
    elif isinstance(func, ast.Name):
        if func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else None
            return (
                bool(node.args)
                and isinstance(mode, ast.Constant)
                and any(m in str(mode.value) for m in _WRITE_MODES)
                and _derives_from(node.args[0], names)
            )
    return False


def _scope_offenders(
    body: list[ast.stmt], names: set[str], source_lines: list[str], offenders: list[str]
) -> None:
    """Walk one scope's statements IN ORDER, tracking what each name holds.

    Per-scope and in order because the names are not global: six suites have
    one function doing `pyproject = REPO_ROOT / "pyproject.toml"` to read the
    version and another doing `pyproject = tmp_path / "pyproject.toml"` to
    write a fixture. A file-wide name set cannot tell those apart and condemns
    the second (#4198's own guard, caught by its first run over the tree).
    """
    names = set(names)

    def report(node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and _write_target(sub, names):
                line = (
                    source_lines[sub.lineno - 1].strip() if sub.lineno <= len(source_lines) else ""
                )
                offenders.append(f"{sub.lineno}: {line}")

    _COMPOUND = (ast.For, ast.AsyncFor, ast.While, ast.If, ast.With, ast.AsyncWith, ast.Try)

    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = set(names)
            # A parameter shadows the constant: `def f(pyproject)`, and above
            # all `def test_x(tmp_path)`.
            for argument in [
                *statement.args.posonlyargs,
                *statement.args.args,
                *statement.args.kwonlyargs,
            ]:
                inner.discard(argument.arg)
            _scope_offenders(statement.body, inner, source_lines, offenders)
            continue
        if isinstance(statement, ast.ClassDef):
            _scope_offenders(statement.body, set(names), source_lines, offenders)
            continue

        if isinstance(statement, _COMPOUND):
            # The header (`for x in <iter>`, `with <ctx>`, `if <test>`) runs in
            # this scope; the bodies get their own pass so an assignment inside
            # one does not leak into the statements after it.
            for attribute in ("iter", "test", "items", "type_comment"):
                header = getattr(statement, attribute, None)
                if isinstance(header, ast.AST):
                    report(header)
                elif isinstance(header, list):
                    for item in header:
                        if isinstance(item, ast.AST):
                            report(item)
            inner = set(names)
            for bound in ast.walk(statement):
                # `for relative in (...)` / `with open(x) as fh` -- the bound
                # name is not a repo path unless something assigns it one.
                if isinstance(bound, ast.Name) and isinstance(bound.ctx, ast.Store):
                    inner.discard(bound.id)
            for attribute in ("body", "orelse", "finalbody"):
                nested = getattr(statement, attribute, None)
                if nested:
                    _scope_offenders(nested, inner, source_lines, offenders)
            for handler in getattr(statement, "handlers", []):
                _scope_offenders(handler.body, inner, source_lines, offenders)
            continue

        report(statement)

        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                if _is_repo_root_expr(statement.value) or (
                    _is_path_expr(statement.value)
                    and _derives_from(statement.value, names)
                    and not _mentions_tmp(statement.value)
                ):
                    names.add(target.id)
                else:
                    names.discard(target.id)


def python_offenders(text: str) -> list[str]:
    """Writes into the repo checkout made by a Python test module."""
    tree = ast.parse(text)
    offenders: list[str] = []
    _scope_offenders(tree.body, set(), text.splitlines(), offenders)
    # A nested scope reports the same statement its parent walked, so the
    # same write can arrive twice; the set keeps the message readable.
    return sorted(set(offenders), key=lambda hit: int(hit.split(":", 1)[0]))


# --------------------------------------------------------------------------
# the tree
# --------------------------------------------------------------------------


def _shell_suites() -> list[Path]:
    return sorted(p for p in TESTS.rglob("*.sh") if p.is_file())


def _python_tests() -> list[Path]:
    return sorted(
        p
        for p in TESTS.rglob("*.py")
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
    )


class TestNoTestWritesIntoTheCheckout:
    """The rule, over the tree."""

    def test_no_shell_suite_writes_into_the_checkout(self):
        offenders = {
            str(path.relative_to(ROOT)): hits
            for path in _shell_suites()
            if (hits := shell_offenders(path.read_text(encoding="utf-8")))
        }
        assert not offenders, (
            "these shell suites write inside the repository checkout, where "
            "every other reader of the tree can see it and where a SIGKILL, a "
            "job timeout or a runner eviction leaves it behind (#4198). Build "
            "the fixture in a directory the suite owns -- "
            "`tests/fixture_checkout.sh` makes a throwaway copy of the tracked "
            f"tree for exactly this: {offenders}"
        )

    def test_no_python_test_writes_into_the_checkout(self):
        offenders = {
            str(path.relative_to(ROOT)): hits
            for path in _python_tests()
            if (hits := python_offenders(path.read_text(encoding="utf-8")))
        }
        assert not offenders, (
            "these tests write to a path derived from the repository root "
            "instead of to `tmp_path` (#4198). A teardown or `finally` restore "
            "is not a mitigation: the window is real while the test runs, and "
            f"an interrupted run never reaches it: {offenders}"
        )

    def test_the_scan_reaches_the_files_it_is_about(self):
        """Otherwise both assertions above could be passing over an empty list."""
        shell = {str(p.relative_to(ROOT)) for p in _shell_suites()}
        python = {str(p.relative_to(ROOT)) for p in _python_tests()}
        assert "tests/test_pr_head_guard_discriminates.sh" in shell
        assert "tests/test_issue_hygiene.sh" in shell
        assert "tests/unit/test_first_principles_contract.py" in python
        assert "tests/unit/test_resources_packaging.py" in python
        assert len(shell) > 20 and len(python) > 100


class TestTheDetectorsAreNotVacuous:
    """Both directions, per #4167: a detector green over nothing is the defect.

    Each case is the real shape, not a paraphrase -- the first two are the
    lines this issue was filed about, copied from the suite as it stood.
    """

    def test_it_catches_a_cp_onto_a_tracked_file(self):
        assert shell_offenders(
            'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'WF="$ROOT_DIR/.github/workflows/ensure_project_hygiene.yml"\n'
            'PRISTINE="$(mktemp)"\n'
            'cp "$PRISTINE" "$WF"\n'
        )

    def test_it_catches_a_planted_file_through_a_variable(self):
        assert shell_offenders(
            'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'PLANTED="$ROOT_DIR/.github/workflows/zz-injection.yml"\n'
            "cat > \"$PLANTED\" <<'YAML'\n"
            "name: unsafe\n"
            "YAML\n"
        )

    def test_it_catches_a_heredoc_program_handed_a_tracked_path(self):
        """The form the other four injections took: the write is in the
        heredoc, so nothing on the command line looks like a destination."""
        assert shell_offenders(
            'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'WF="$ROOT_DIR/.github/workflows/ensure_project_hygiene.yml"\n'
            "python3 - \"$WF\" <<'PY'\n"
            "import sys\n"
            'open(sys.argv[1], "w").write("broken")\n'
            "PY\n"
        )

    def test_it_catches_a_write_routed_through_a_helper_function(self):
        """How two of the three tracked files in #4198 were actually patched.

        Neither line is a write on its own: the call site runs a function, and
        the body writes to `$1`, which means nothing until the argument is
        bound. The indirection is the thing to catch -- a rule that stops at
        the literal `cp "$PRISTINE" "$WF"` is one refactor from vacuous.
        """
        assert shell_offenders(
            'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'WF="$ROOT_DIR/.github/workflows/ensure_project_hygiene.yml"\n'
            "_patch_last_ref() {\n"
            '  local file="$1" replacement="$2"\n'
            '  python3 - "$file" "$replacement" <<\'PY\'\n'
            "import sys\n"
            'open(sys.argv[1], "w").write(sys.argv[2])\n'
            "PY\n"
            "}\n"
            "_patch_last_ref \"$WF\" 'anything'\n"
        )

    def test_a_helper_writing_only_to_a_path_the_suite_owns_is_quiet(self):
        """The same indirection, used correctly: read the checkout, write $TMP."""
        assert not shell_offenders(
            'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'TMP="$(mktemp -d)"\n'
            "_revert() {\n"
            '  local src="$1" dst="$2"\n'
            '  cp "$src" "$dst"\n'
            "}\n"
            '_revert "$ROOT_DIR/scripts/agents/create_issue.sh" "$TMP/retired.sh"\n'
        )

    def test_it_catches_an_rm_of_a_repo_path(self):
        assert shell_offenders(
            'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'rm -f "$ROOT_DIR/.github/workflows/zz-injection.yml"\n'
        )

    @pytest.mark.parametrize(
        "script",
        [
            # The correct shape: a directory the suite owns.
            'TMP="$(mktemp -d)"\ncp "$ROOT_DIR/scripts/agents/lib" "$TMP/lib"\n',
            'FIXTURE="$(mktemp -d)"\ncat > "$FIXTURE/.github/workflows/x.yml" <<\'Y\'\na\nY\n',
            # Reading from the checkout, which is the whole job of these suites.
            'source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"\n',
            'WF_TEXT="$(cat "$ROOT_DIR/.github/workflows/x.yml")"\n',
            'bash "$ROOT_DIR/scripts/agents/create_issue.sh" --title x\n',
            # Writes outside any checkout.
            'echo hi > /tmp/guard.out\nrm -f "$PRISTINE"\n',
        ],
    )
    def test_it_stays_quiet_on_the_legitimate_shapes(self, script):
        assert not shell_offenders(
            'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n' + script
        )

    def test_it_catches_a_python_write_through_a_root_constant(self):
        assert python_offenders(
            "from pathlib import Path\n"
            "REPO_ROOT = Path(__file__).resolve().parents[2]\n"
            "def test_x():\n"
            '    (REPO_ROOT / "k8s" / "secret.yaml").write_text("api-key: x")\n'
        )

    def test_it_catches_a_write_through_an_intermediate_name(self):
        """`artifact_dir = REPO_ROOT / ".claude-pr"` then `artifact.write_text`
        -- the shape `test_first_principles_contract.py` had."""
        assert python_offenders(
            "from pathlib import Path\n"
            "REPO_ROOT = Path(__file__).resolve().parents[2]\n"
            "def test_x():\n"
            '    artifact_dir = REPO_ROOT / ".claude-pr"\n'
            '    artifact = artifact_dir / "CLAUDE.md"\n'
            "    artifact_dir.mkdir()\n"
            '    artifact.write_text("x")\n'
        )

    def test_it_catches_an_unlink_in_a_finally(self):
        """A `finally` restore is the mitigation this rule rejects."""
        assert python_offenders(
            "from pathlib import Path\n"
            "REPO_ROOT = Path(__file__).resolve().parents[2]\n"
            "def test_x():\n"
            "    try:\n"
            "        pass\n"
            "    finally:\n"
            '        (REPO_ROOT / "k8s" / "secret.yaml").unlink(missing_ok=True)\n'
        )

    @pytest.mark.parametrize(
        "body",
        [
            # tmp_path is the correct destination.
            '    (tmp_path / "x.yml").write_text("a")\n',
            # Reading the checkout is the point of most of these guards.
            '    text = (REPO_ROOT / "CLAUDE.md").read_text()\n',
            '    files = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))\n',
            # Copying OUT of the checkout into a fixture.
            '    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")\n',
            # A path built from the root and handed to a subprocess read.
            '    subprocess.run(["bash", str(REPO_ROOT / "tests" / "x.sh")])\n',
        ],
    )
    def test_the_python_detector_stays_quiet_on_reads_and_tmp_paths(self, body):
        assert not python_offenders(
            "import shutil, subprocess\n"
            "from pathlib import Path\n"
            "REPO_ROOT = Path(__file__).resolve().parents[2]\n"
            "def test_x(tmp_path):\n" + body
        )


class TestTheGuardSuiteNeedsNoSeamInTheGuard:
    """#4198 criterion 3, in the form that makes the criterion unnecessary.

    Pointing a guard at a fixture tree is usually done with a root override,
    and an override is a switch that can be left on: a guard reading
    `os.environ.get("GUARD_ROOT", REPO_ROOT)` passes on the real repository
    whenever something in the environment says otherwise. There is no such
    switch here. `test_pull_request_target_safety.py` resolves its root from
    its OWN location, so a COPY of the guard examines the copy -- the fixture
    run and the production run are the same code reading the tree it is in.
    """

    GUARD = ROOT / "tests" / "unit" / "test_pull_request_target_safety.py"
    SUITE = ROOT / "tests" / "test_pr_head_guard_discriminates.sh"

    def test_the_guard_resolves_its_root_from_its_own_file(self):
        text = self.GUARD.read_text(encoding="utf-8")
        assert "ROOT = Path(__file__).resolve().parents[2]" in text

    def test_the_guard_reads_no_environment_variable(self):
        """So nothing outside the file can redirect what it examines."""
        tree = ast.parse(self.GUARD.read_text(encoding="utf-8"))
        source = ast.dump(tree)
        for seam in ("environ", "getenv"):
            assert seam not in source, (
                f"the guard consults {seam}: a root override is a switch that "
                "can be left on, and the fixture run needs none -- the suite "
                "copies the guard instead (#4198)"
            )

    def test_the_suite_builds_a_fixture_copy_and_runs_the_guard_inside_it(self):
        text = self.SUITE.read_text(encoding="utf-8")
        assert "fixture_checkout.sh" in text
        assert 'fixture_checkout "$ROOT_DIR" "$FIXTURE"' in text
        assert 'cd "$FIXTURE"' in text, (
            "the guard must be run from the fixture copy; running the "
            "checkout's own copy would examine the checkout (#4198)"
        )

    def test_the_suite_proves_the_real_checkout_survived(self):
        """The assertion that makes the fix executed rather than inspected."""
        text = self.SUITE.read_text(encoding="utf-8")
        assert "PORCELAIN_BEFORE" in text and "PORCELAIN_AFTER" in text
        assert "status --porcelain" in text

    def test_the_only_cleanup_the_suite_needs_is_rm_of_its_own_fixture(self):
        """A trap that RESTORES is the shape #4198 rejects: it has to run to
        be correct, and no trap runs on SIGKILL."""
        text = self.SUITE.read_text(encoding="utf-8")
        traps = re.findall(r"^\s*trap\s+(.+?)\s+EXIT", text, re.MULTILINE)
        assert traps, "the suite no longer cleans up its fixture at all"
        for trap in traps:
            assert "rm -rf" in trap and "cp " not in trap, (
                f"the EXIT trap {trap!r} restores state rather than discarding "
                "it, so the correctness of the run depends on the trap firing "
                "(#4198)"
            )


class TestTheSharedFixtureHelperIsTheOneSource:
    """One source per decision (D-066): how a suite gets a tree it may break.

    Two suites answering this their own way is how the same defect returns
    through the second copy, so the copy lives in `tests/fixture_checkout.sh`
    and callers call it.
    """

    HELPER = ROOT / "tests" / "fixture_checkout.sh"

    def test_the_helper_exists_and_is_both_sourceable_and_runnable(self):
        text = self.HELPER.read_text(encoding="utf-8")
        assert "fixture_checkout()" in text
        assert 'if [[ "${BASH_SOURCE[0]}" == "${0}" ]]' in text, (
            "the helper must also run as a command, so the Python side can "
            "call it rather than reimplementing the copy"
        )

    def test_it_refuses_a_destination_inside_the_tree_it_copies(self):
        """A fixture inside the checkout is the mutation this all prevents."""
        text = self.HELPER.read_text(encoding="utf-8")
        assert "is inside root" in text

    def test_it_fails_loudly_on_an_incomplete_copy(self):
        """An empty fixture is a tree every guard passes over -- the #4195
        vacuous-green shape, arriving by a different road."""
        text = self.HELPER.read_text(encoding="utf-8")
        assert "_FIXTURE_REQUIRED" in text
        assert "listed no files" in text
