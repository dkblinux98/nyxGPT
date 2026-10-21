"""A ``pipefail`` pipeline must not report its reader's early exit (#4192).

The defect this file exists for was a *required check going red on a green
commit*. `macos-brew-smoke.yml`'s non-vacuity assertion for #4043 read::

    brew services list | grep -q "$KEG" || { echo "::error::...measuring
    nothing"; exit 1; }

``grep -q`` leaves on the first match. brew's next write then takes ``EPIPE``
(``Broken pipe @ rb_sys_fail_on_write - <STDOUT>`` is in run 38071858945's log,
directly under the line showing ``nyxgpt-api@3.0.0rc`` matched), and
``set -o pipefail`` promotes that to the pipeline's status -- so the assertion
fired *because it had succeeded*. The same head had passed at 38070775763: it
is a race on whether the producer still has a write pending when the reader
leaves, which is precisely a check whose colour is not evidence about the
commit.

Three measurements, because each answers a different question:

1. **The mechanism, executed** -- that a producer really does die this way
   under ``pipefail``, that a *builtin* producer dies the same way (the trap
   this round fell into: a 1.2 MB single-line payload survives, because
   ``grep`` must reach the newline before it can match and therefore drains
   it; the same bytes as 200k lines exit 141), and that the replacements this
   sweep applied tree-wide -- ``grep PAT >/dev/null``, ``sed -n '1,Np'`` and
   ``grep -q PAT <<<"$text"`` -- preserve the reader's status and output while
   draining. The whole sweep rests on that equivalence, so it is run rather
   than asserted.
2. **The repaired steps, executed, in both directions** -- the real shell out of
   the workflow (never re-typed here) against a stub producer that loses the
   race deliberately. The retired form must fail; the shipped form must pass.
3. **The guard** -- that `sigpipe_pipeline_guard` finds a planted hazard, does
   not flag the accepted forms, and reports nothing on the tree as it stands.

Direction 2 matters most: a test that only exercises the fixed form passes on
any machine where the race does not reproduce, which is how this reached the
release branch (#3753's lesson, ledger D-006).

**Round 5 added a fourth** (`4. The class, not the idiom`), because the three
above all passed while the identical defect reddened the same workflow again
(run 39679850952, `candidate-upgrade`)::

    tar -xzOf "$tgz" "$member" | awk -F'"' '/^version =/{print $2; exit}'

``awk``'s ``exit`` is ``grep -q`` under another name, and the guard knew only
``grep -q`` and ``head``. Worse, ``split_pipeline`` counted quotes
left-to-right, so a pipeline inside ``"$( ... )"`` -- the dominant idiom in
this tree -- read as having no pipe at all, which is why the sweep's "79
hazards, now clean" reported 0 where the corrected scan reports 38. A gate
named for a behaviour and enforcing one spelling of it is this issue's class
with the gate itself as the instance, so this section measures the *class*:
every reader in `EARLY_READERS`, each one executed, and the enumeration itself
pinned so a documented rule cannot sit unimplemented.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "agents" / "lib" / "sigpipe_pipeline_guard.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "macos-brew-smoke.yml"

sys.path.insert(0, str(GUARD.parent))

import sigpipe_pipeline_guard as guard  # noqa: E402

#: How many lines the stub producer writes. Far past any pipe buffer, so the
#: producer is guaranteed to still be writing when a reader that stops at the
#: first match has already gone.
STUB_LINES = 200_000


# ---------------------------------------------------------------------------
# 1. The mechanism, and the equivalence the sweep rests on
# ---------------------------------------------------------------------------


def _pipefail(snippet: str) -> int:
    """Run ``snippet`` under ``set -o pipefail`` and return its status."""
    completed = subprocess.run(
        ["bash", "-c", "set -o pipefail\n" + snippet],
        capture_output=True,
        text=True,
    )
    return completed.returncode


PRODUCER = f"seq 1 {STUB_LINES}"


def test_an_external_producer_dies_of_its_readers_early_exit() -> None:
    """The defect itself: a successful match reported as a failed pipeline."""
    assert _pipefail(f"{PRODUCER} | grep -q '^1$'") == 141
    assert _pipefail(f"{PRODUCER} | head -1 >/dev/null") == 141


def test_a_builtin_producer_dies_the_same_way() -> None:
    """``printf "$text" | grep -q`` is NOT a fix, and this is why.

    The first draft of this sweep used exactly that idiom, on the strength of
    a probe that returned 0 for a 1.2 MB payload. The probe was wrong about
    its own premise: that payload was ONE long line, so grep could not decide
    without reading to the newline and drained it. The same byte count as
    200k lines takes the builtin straight into EPIPE.

    Kept as a test because the conclusion is counter-intuitive and the whole
    guard rests on it: there is no safe producer, only a safe reader.
    """
    one_long_line = _pipefail(
        f"text=\"$({PRODUCER} | tr '\\n' ' ')\"\n" "printf '%s\\n' \"$text\" | grep -q '^1 '"
    )
    assert one_long_line == 0, "a single line must be drained to be matched"

    many_lines = _pipefail(f'text="$({PRODUCER})"\n' "printf '%s\\n' \"$text\" | grep -q '^1$'")
    assert many_lines == 141, "the reader is what decides, not the producer"


def test_a_here_string_cannot_lose_the_race() -> None:
    """The accepted no-pipe form, on the payload that breaks the piped one."""
    assert _pipefail(f'text="$({PRODUCER})"\ngrep -q \'^1$\' <<<"$text"') == 0
    assert _pipefail(f'text="$({PRODUCER})"\ngrep -q \'^nope$\' <<<"$text"') == 1


def test_grep_to_devnull_keeps_the_status_and_drains() -> None:
    """``grep PAT >/dev/null`` is ``grep -q`` without the early exit."""
    assert _pipefail(f"{PRODUCER} | grep '^1$' >/dev/null") == 0
    assert _pipefail(f"{PRODUCER} | grep '^nope$' >/dev/null") == 1
    # ...and the same for the option spellings the sweep rewrote.
    assert _pipefail(f"{PRODUCER} | grep -Fx '1' >/dev/null") == 0
    assert _pipefail(f"{PRODUCER} | grep -E '^(1)$' >/dev/null") == 0


def test_sed_range_keeps_the_text_and_drains() -> None:
    """``sed -n '1,Np'`` is ``head -N`` without the early exit."""
    out = subprocess.run(
        ["bash", "-c", f"set -o pipefail\n{PRODUCER} | sed -n '1,3p'"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0
    assert out.stdout.split() == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# 2. The repaired workflow steps, executed, in both directions
# ---------------------------------------------------------------------------


def _step_script(step_name: str) -> str:
    """The real ``run:`` body of a step, by name."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                return str(step["run"])
    raise AssertionError(f"{WORKFLOW} has no step named {step_name!r} any more")


def _block(script: str, first_line_contains: str) -> str:
    """The compound statement starting at ``first_line_contains``.

    Sliced out of the workflow rather than restated, for the reason
    `test_macos_restart_wait._shell_function` is: a copy maintained here would
    be the thing under test drifting away from the thing that ships. Ends at
    the first line that is a bare ``}`` -- the close of the ``|| { ... }``
    the assertion reports through.
    """
    lines = script.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if first_line_contains in line),
        None,
    )
    assert start is not None, f"the step no longer contains {first_line_contains!r}"
    for end in range(start, len(lines)):
        if lines[end].strip() == "}":
            return textwrap.dedent("\n".join(lines[start : end + 1]))
    raise AssertionError(f"the block at {first_line_contains!r} is not closed")


def _stub_producer(tmp_path: Path, name: str, payload_line: str) -> Path:
    """A ``$name`` on PATH that behaves like brew/launchctl do here.

    Python, not bash: the point is a producer whose writes can fail, which a
    shell builtin's cannot. It writes the matching line FIRST and then keeps
    writing, so a reader that stops at the first match always leaves while
    this process still has output pending -- the race, made deterministic.

    Exits 141 on a closed pipe, as a SIGPIPE-killed program does.
    """
    stub = tmp_path / name
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"payload = {payload_line!r}\n"
        "try:\n"
        "    sys.stdout.write(payload + '\\n')\n"
        "    sys.stdout.flush()\n"
        f"    for i in range({STUB_LINES}):\n"
        "        sys.stdout.write('filler-%d\\n' % i)\n"
        "    sys.stdout.flush()\n"
        "except BrokenPipeError:\n"
        "    sys.exit(141)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "harness.sh"
    harness.write_text(script, encoding="utf-8")
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run(["bash", str(harness)], capture_output=True, text=True, env=env)


KEG = "nyxgpt-api@3.0.0rc"
BREW_STEP = "Self-heal can restart the candidate's own service (#4043)"


@pytest.fixture()
def brew_stub(tmp_path: Path) -> Path:
    _stub_producer(tmp_path, "brew", f"{KEG}        none")
    return tmp_path


def test_the_retired_brew_form_fails_on_a_listing_that_matches(brew_stub: Path) -> None:
    """The failure, reproduced: the match succeeds and the step still exits 1.

    This is run 38071858945's job in miniature -- the name IS in the listing,
    and the retired pipeline reports otherwise.
    """
    result = _run(
        "set -euo pipefail\n" f"KEG={KEG}\n"
        # The retired form, reconstructed verbatim from the diff this issue
        # replaced. Kept here, not in the workflow, so the shipped file holds
        # only the form that works.
        'brew services list | grep -q "$KEG" || {\n'
        '  echo "measuring nothing"\n'
        "  exit 1\n"
        "}\n"
        'echo "reached the assertions"\n',
        brew_stub,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "measuring nothing" in result.stdout
    assert "reached the assertions" not in result.stdout


def test_the_shipped_brew_form_passes_the_same_listing(brew_stub: Path) -> None:
    """And the fix, on the workflow's own shell, against the same stub."""
    block = _block(_step_script(BREW_STEP), 'SERVICES="$(brew services list)"')
    assert "grep -q" in block, "the block no longer contains the assertion under test"
    result = _run(
        "set -euo pipefail\n" f"KEG={KEG}\n" + block + '\necho "reached the assertions"\n',
        brew_stub,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reached the assertions" in result.stdout
    assert "measuring nothing" not in result.stdout


LOADED_STEP = "Stop: prove `nyxgpt ops stop ollama-logs` boots out the ollama log watcher"
LABEL = "com.nyxgpt.ollama-logs"


def _loaded_harness(script_body: str) -> str:
    return (
        "set -uo pipefail\n"
        + script_body
        + f'\nif loaded {LABEL}; then echo LOADED; else echo "NOT LOADED"; fi\n'
    )


@pytest.fixture()
def launchctl_stub(tmp_path: Path) -> Path:
    _stub_producer(tmp_path, "launchctl", f"4242\t0\t{LABEL}")
    return tmp_path


def test_the_retired_loaded_helper_reports_a_loaded_service_as_absent(
    launchctl_stub: Path,
) -> None:
    """The sibling instance, and the worse one.

    `loaded()` is read as a predicate twice in that step: a false "not loaded"
    first SKIPS the runtime half with a warning, then makes the
    did-the-stop-work assertion PASS on a service that survived. Nothing goes
    red -- the gate just stops measuring.
    """
    result = _run(
        _loaded_harness('loaded() { launchctl list 2>/dev/null | grep -q "$1"; }'),
        launchctl_stub,
    )
    assert "NOT LOADED" in result.stdout, result.stdout + result.stderr


def test_the_shipped_loaded_helper_sees_it(launchctl_stub: Path) -> None:
    """The workflow's own `loaded()`, against the same stub."""
    script = _step_script(LOADED_STEP)
    match = re.search(r"^(\s*)loaded\(\)\s*\{", script, re.MULTILINE)
    assert match, "the step no longer defines loaded()"
    indent = match.group(1)
    rest = script[match.start() :].splitlines(keepends=True)
    body = [rest[0]]
    for line in rest[1:]:
        body.append(line)
        if line.rstrip("\n") == f"{indent}}}":
            break
    else:  # pragma: no cover - defended by the assertion below
        raise AssertionError("loaded() is not closed at its own indentation")
    result = _run(_loaded_harness(textwrap.dedent("".join(body))), launchctl_stub)
    assert "LOADED" in result.stdout, result.stdout + result.stderr
    assert "NOT LOADED" not in result.stdout


# ---------------------------------------------------------------------------
# 3. The guard
# ---------------------------------------------------------------------------

HAZARD_YAML = """\
name: planted
on: [push]
jobs:
  planted:
    runs-on: ubuntu-latest
    steps:
      - name: A pipefail pipeline that reports its reader's early exit
        run: |
          set -euo pipefail
          brew services list | grep -q "$KEG" || exit 1
"""


def test_the_guard_rejects_a_planted_workflow_hazard(tmp_path: Path) -> None:
    planted = tmp_path / "planted.yml"
    planted.write_text(HAZARD_YAML, encoding="utf-8")
    hazards = guard.scan([planted])
    assert len(hazards) == 1
    assert hazards[0].producer == "brew"
    assert guard.main([str(planted)]) == 1


@pytest.mark.parametrize(
    "snippet",
    [
        'set -euo pipefail\nbrew services list | grep -q "$KEG"',
        "set -euo pipefail\nkubectl get pods | head -1",
        # The reader may sit on a continuation line; a scanner that only looks
        # at single physical lines attributes the producer to nothing and
        # passes. That is how the first draft of this guard read 208 files
        # clean with the live defect in one of them.
        'set -euo pipefail\nif gh api x --paginate \\\n  | jq -s "." \\\n  | grep -q true; then :; fi',
        "set -euo pipefail\nnyxgpt ops status | grep -m1 'Install mode'",
        "set -euo pipefail\nsed -n '1,$p' f | grep --quiet x",
        # The builtin case, which the corrected rule flags.
        'set -euo pipefail\necho "$status" | grep -q x',
    ],
)
def test_the_guard_flags_every_hazard_spelling(snippet: str, tmp_path: Path) -> None:
    assert guard.find_hazards(snippet, tmp_path / "s.sh"), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        # 1. The accepted replacements.
        "set -euo pipefail\nbrew services list | grep x >/dev/null",
        "set -euo pipefail\nkubectl get pods | sed -n '1p'",
        # 2. Reader drains anyway.
        "set -euo pipefail\nbrew services list | grep -c x",
        "set -euo pipefail\nbrew services list | tail -1",
        "set -euo pipefail\nbrew services list | wc -l",
        # 3. No pipeline at all -- including the here-string form the
        #    in-memory cases were rewritten to.
        'set -euo pipefail\ngrep -q x file\ngrep -q x <<< "$text"',
        'set -euo pipefail\ngrep -q x <<<"$status"',
        # 4. Status deliberately discarded: the reader already printed the
        #    value the caller wanted.
        'set -euo pipefail\nW="$(ls dist/*.whl | head -1)" || true',
        # 5. pipefail not in effect, so the pipeline's status IS the reader's.
        "set -eu\nbrew services list | grep -q x",
        "set -euo pipefail\nset +o pipefail\nbrew services list | grep -q x",
        # 6. `||` is not a pipe.
        "set -euo pipefail\nbrew services list || grep -q x file",
        # 7. A `|` inside quotes is data, not a pipeline.
        "set -euo pipefail\nkubectl get pods -o jsonpath='{.a|b}' | grep x >/dev/null",
    ],
)
def test_the_guard_accepts_the_safe_forms(snippet: str, tmp_path: Path) -> None:
    assert guard.find_hazards(snippet, tmp_path / "s.sh") == [], snippet


def test_the_guard_reports_the_line_it_found(tmp_path: Path) -> None:
    script = tmp_path / "s.sh"
    script.write_text(
        "set -euo pipefail\n" "echo fine\n" 'brew services list | grep -q "$KEG"\n',
        encoding="utf-8",
    )
    (hazard,) = guard.scan_shell(script)
    assert hazard.line == 3
    assert "brew services list" in hazard.format()


def test_the_tree_has_no_such_pipeline() -> None:
    """Every workflow, and every shell script the gates run.

    The sweep for #4192: 79 pipelines across 34 files, every one of them a
    gate workflow or an agent script, each now either reading from a
    here-string/file or using a reader that drains. Measured by running this
    guard against the pre-fix head (`71e4b29e`), not estimated.
    """
    hazards = guard.scan(guard.default_paths())
    assert hazards == [], "\n".join(h.format() for h in hazards)


def test_the_guard_covers_the_surfaces_it_claims() -> None:
    """A guard pointed at nothing passes everything."""
    paths = guard.default_paths()
    assert len([p for p in paths if p.suffix in (".yml", ".yaml")]) > 50
    assert len([p for p in paths if p.suffix == ".sh"]) > 50
    assert WORKFLOW.relative_to(REPO_ROOT) in paths


# ---------------------------------------------------------------------------
# 4. The class, not the idiom (round 5)
# ---------------------------------------------------------------------------

#: One snippet per entry in ``guard.EARLY_READERS``, keyed by that entry, so
#: the parametrisation and the enumeration cannot drift apart. Each is run
#: under ``pipefail`` against a 200k-line producer AND handed to the guard:
#: the hazard has to be real before it is worth flagging.
EARLY_READER_CASES: dict[str, str] = {
    "head": f"{PRODUCER} | head -1 >/dev/null",
    "grep -q / -l / -m N": f"{PRODUCER} | grep -q '^1$'",
    "sed ... q / Q": f"{PRODUCER} | sed -n '1p;q'",
    "awk ... exit": f"{PRODUCER} | awk '/^1$/{{print; exit}}' >/dev/null",
    "read": f"{PRODUCER} | read -r _first",
    "while/until read ... break": (
        f'{PRODUCER} | while read -r line; do echo "$line" >/dev/null; break; done'
    ),
}


def test_the_cases_cover_every_reader_the_guard_claims() -> None:
    """A rule in the docs and not in the enumeration is not enforced.

    The reverse of the defect this section exists for: `awk ... exit` was a
    hazard nobody had written down. Now the list is the contract, and a new
    entry with no executed case fails here rather than passing silently.
    """
    assert set(EARLY_READER_CASES) == set(guard.EARLY_READERS)


@pytest.mark.parametrize("reader", list(EARLY_READER_CASES))
def test_every_early_reader_really_kills_its_producer(reader: str) -> None:
    """Executed, not assumed: each spelling fails the pipeline it succeeds in."""
    assert _pipefail(EARLY_READER_CASES[reader]) != 0, reader


@pytest.mark.parametrize("reader", list(EARLY_READER_CASES))
def test_the_guard_flags_every_early_reader(reader: str, tmp_path: Path) -> None:
    hazards = guard.find_hazards(
        "set -euo pipefail\n" + EARLY_READER_CASES[reader] + "\n", tmp_path / "s.sh"
    )
    assert hazards, reader


@pytest.mark.parametrize(
    "snippet",
    [
        # `exit` reached only from END runs after EOF: the input is drained.
        f"{PRODUCER} | awk '/^1$/{{v=$0}} END{{print v; exit (v == \"\")}}'",
        # The replacement this sweep applied: first match held, printed at END.
        f"{PRODUCER} | awk '!v && /^1$/{{v=$0}} END{{print v}}'",
        # `q` as DATA, not as a command.
        f"{PRODUCER} | sed 's/q/x/' >/dev/null",
        f"{PRODUCER} | sed -n '/queued/p'",
        # A `while read` loop with no `break` drains, like any other reader.
        f'{PRODUCER} | while read -r line; do : "$line"; done',
    ],
)
def test_the_draining_forms_are_accepted_and_really_drain(snippet: str, tmp_path: Path) -> None:
    """Both halves in one test: the guard allows it *and* bash agrees."""
    assert guard.find_hazards("set -euo pipefail\n" + snippet + "\n", tmp_path / "s.sh") == []
    assert _pipefail(snippet) == 0, snippet


def test_the_awk_replacement_prints_what_the_retired_form_printed() -> None:
    """The equivalence the sweep rests on, for the `awk` rewrites."""
    stamped = "python3 -c \"print('\\n'.join(['version = \\\"9.9.9\\\"'] + ['pad'] * 50000))\""
    retired = subprocess.run(
        ["bash", "-c", f"{stamped} | awk -F'\"' '/^version =/{{print $2; exit}}'"],
        capture_output=True,
        text=True,
    )
    shipped = subprocess.run(
        ["bash", "-c", f"{stamped} | awk -F'\"' '!v && /^version =/{{v=$2}} END{{print v}}'"],
        capture_output=True,
        text=True,
    )
    assert shipped.stdout == retired.stdout == "9.9.9\n"


# --- The pipeline the splitter could not see -------------------------------


def test_a_pipe_inside_a_command_substitution_is_a_pipeline(tmp_path: Path) -> None:
    """The silent pass: ``"$( a | reader )"`` scanned as having no pipe.

    Counting quotes left-to-right makes the second ``"`` CLOSE the assignment's
    own quote, so the ``|`` looks quoted. Bash starts quoting afresh inside
    ``$( )``. This is the exact shape of the third live copy of the defect, and
    the first version of this guard passed it.
    """
    line = 'DECLARED="$(tar -xzOf "$tgz" "$member" | awk -F\'"\' \'/^v/{print $2; exit}\')"'
    assert len(guard.split_pipeline(line)) == 2
    hazards = guard.find_hazards("set -euo pipefail\n" + line + "\n", tmp_path / "s.sh")
    assert len(hazards) == 1
    assert hazards[0].producer == "tar"


@pytest.mark.parametrize(
    "line",
    [
        # Nested substitution, still a pipeline.
        'V="$(echo "$(date)" | head -1)"',
        # Backticks open a context of their own too.
        'V=`ls "$d" | head -1`',
    ],
)
def test_substitution_shapes_are_split(line: str, tmp_path: Path) -> None:
    assert guard.find_hazards("set -euo pipefail\n" + line + "\n", tmp_path / "s.sh")


def test_a_quoted_pipe_is_still_not_a_pipeline(tmp_path: Path) -> None:
    """The correction must not have made every `|` a pipeline."""
    for line in (
        "kubectl get pods -o jsonpath='{.a|b}' | grep x >/dev/null",
        'echo "a|b" | grep x >/dev/null',
        "awk -F'|' '{print $1}' file >/dev/null",
    ):
        assert guard.find_hazards("set -euo pipefail\n" + line + "\n", tmp_path / "s.sh") == []


# --- The live fix: one helper, executed both ways --------------------------

HELPER = REPO_ROOT / "scripts" / "tarball_declared_version.sh"
RETIRED_READER = """awk -F'"' '/^version =/{print $2; exit}'"""


def _sdist(tmp_path: Path, name: str, version: str, pad: int = 200_000) -> Path:
    """A real sdist whose pyproject is long enough to still be writing."""
    root = tmp_path / f"{name}-{version}"
    root.mkdir(parents=True, exist_ok=True)
    body = ["[project]", f'version = "{version}"']
    body += [f"# pad {i}" for i in range(pad)]
    # A later `version =` under another table: the helper must take the first.
    body += ["[tool.other]", 'version = "0.0.0"']
    (root / "pyproject.toml").write_text("\n".join(body) + "\n", encoding="utf-8")
    tarball = tmp_path / f"{name}-{version}.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(tmp_path), root.name],
        check=True,
        capture_output=True,
    )
    return tarball


def test_the_retired_declared_version_form_dies_on_the_right_answer(tmp_path: Path) -> None:
    """Run 39679850952 in miniature: EPIPE on a tarball that declares correctly."""
    tarball = _sdist(tmp_path, "nyxgpt-api", "9.9.9")
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            f'v="$(tar -xzOf "{tarball}" "nyxgpt-api-9.9.9/pyproject.toml" '
            f'| {RETIRED_READER})"\n'
            'echo "got=$v"\n',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "got=" not in result.stdout


def test_the_helper_reads_the_same_tarball_and_succeeds(tmp_path: Path) -> None:
    tarball = _sdist(tmp_path, "nyxgpt-api", "9.9.9")
    result = subprocess.run(["bash", str(HELPER), str(tarball)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "9.9.9", "the FIRST version line, not [tool.other]'s"


def test_the_helper_refuses_a_tarball_that_declares_nothing(tmp_path: Path) -> None:
    """An empty answer must not compare equal to an expected version."""
    root = tmp_path / "nyxgpt-api-1.0.0"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    tarball = tmp_path / "nyxgpt-api-1.0.0.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(tmp_path), root.name],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(["bash", str(HELPER), str(tarball)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "declares no version" in result.stderr


def test_every_step_that_asks_the_version_asks_the_one_helper() -> None:
    """D-066: the decision lives in one place, and the copies are gone.

    Three steps of `macos-brew-smoke.yml` each made this decision, and all
    three made it the same wrong way -- which is why the fix consolidates
    instead of correcting them one at a time.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert RETIRED_READER not in text, "a copy of the retired reader is back"
    assert text.count("scripts/tarball_declared_version.sh") >= 3
    # ...and no step reaches into a tarball for a version by hand any more.
    for _, line in guard.logical_lines(text):
        if "tar -xzOf" in line and "pyproject.toml" in line:
            raise AssertionError(f"a step still reads the version itself: {line.strip()}")


# --- The report has to point at the step ----------------------------------

TWO_STEP_YAML = """\
name: planted
on: [push]
jobs:
  planted:
    runs-on: ubuntu-latest
    steps:
      - name: An earlier step opening exactly the same way
        run: |
          set -euo pipefail
          echo harmless
      - name: The step with the hazard in it
        run: |
          set -euo pipefail
          brew services list | head -1
"""


def test_the_hazard_is_reported_at_its_own_line(tmp_path: Path) -> None:
    """Not at the first body in the file that happens to start the same way.

    Every `run:` here opens with `set -euo pipefail`, so anchoring on the
    body's first line without a cursor reported all 59 steps of
    `macos-brew-smoke.yml` at one line -- which cost this round a diagnosis.
    A wrong line in a report reads exactly like a right one.
    """
    planted = tmp_path / "planted.yml"
    planted.write_text(TWO_STEP_YAML, encoding="utf-8")
    (hazard,) = guard.scan([planted])
    lines = TWO_STEP_YAML.splitlines()
    assert lines[hazard.line - 1].strip() == "brew services list | head -1"


def test_every_step_body_resolves_to_its_own_first_line() -> None:
    """The same property over every workflow in the repository."""
    sys.path.insert(0, str(GUARD.parent))
    from workflow_script_guard import iter_script_steps, workflow_files  # noqa: PLC0415

    misattributed = []
    for path in workflow_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for step in iter_script_steps(path):
            first = next((ln for ln in step.script.splitlines() if ln.strip()), "")
            if not first:
                continue
            here = lines[step.line - 1].strip()
            # A folded scalar (`run: >-`) exists in no single line, so the
            # step's own anchor is the answer there.
            if not (
                here == first.strip()
                or here.endswith(first.strip())
                or here.startswith(("- name:", "run:", "script:"))
            ):
                misattributed.append(f"{path.name}:{step.line} [{step.step}] -> {here[:60]}")
    assert misattributed == [], "\n".join(misattributed)


# --- The boundary, measured rather than assumed ---------------------------


def test_no_interpreter_reader_in_this_tree_stops_early() -> None:
    """The one hazard shape a textual guard cannot see.

    `producer | python3 script.py` is only a hazard if the script stops
    reading before EOF. Nothing in this tree does -- `sys.stdin.read()`
    throughout -- and the docstring says so, so the claim is checked here
    instead of being left as a comment that quietly stops being true.
    """
    early = []
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if re.search(r"stdin\.readline\(|next\(\s*sys\.stdin", line):
                early.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert early == [], "\n".join(early)
