"""The Homebrew wrappers' shebang has to be at byte 0 (#4043).

Every file a formula writes with Ruby's squiggly heredoc (`<<~`) is written
with the indentation of its *least*-indented line removed. The wrappers nested
a shell heredoc inside that heredoc:

    (bin/"nyxgpt-api").write <<~EOS
      #!/bin/bash
      ...
        IFS=$'\\t' read -r HOST PORT < <("$SYS_PY" - <<'PY'
    import configparser          # <- column 0, because a `<<'PY'` body must be
    ...
    PY
    )
      fi

A `<<'PY'` body cannot be indented, so the least-indented line of the OUTER
heredoc was column 0 -- and `<<~` then stripped nothing. The wrapper was
written with `      #!/bin/bash`, six spaces in.

That is not a shebang, and Homebrew notices. `Cleaner#clean_dir` re-stamps
every file in `bin`:

    def executable_path?(path) = Utils::Path.text_executable?(path) || path.executable?
    perms = if executable_path?(path) then 0555 else 0444 end
    def text_executable?(path) = /\\A#!\\s*\\S+/.match?(path.open("r") { _1.read(1024) })

so the formula's own `chmod 0755` was undone to 0444 on every install --
`==> chmod 0755 .../bin/nyxgpt-api` followed by `-r--r--r-- nyxgpt-api` in the
same job. The `service` block runs the wrapper as `["/bin/bash", opt_bin/...]`,
which needs neither the shebang nor the exec bit, so no running stack ever
noticed; what noticed were the two macos-brew-smoke gates that exec the
wrapper directly (`test -x "$KEG/bin/nyxgpt-api"`, red since 2026-08-23, and
`nohup "$old_keg/bin/nyxgpt-api"` -> Permission denied), and #3406's launchd
failure 78.

These tests guard the class, not the one spelling: for EVERY `write <<~`
heredoc in all four formula sources, the first line has to be among the
least-indented -- which is exactly the property a nested column-0 heredoc
breaks -- and the two wrappers additionally have to satisfy Homebrew's own
`text_executable?` regex. The config readers the wrappers now call are
rendered and RUN here, because a wrapper that starts correctly and then reads
the wrong port is no better than the mode bug.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# All four copies: the file:// tap formulas installed from a checkout, and the
# templates `build_homebrew_artifacts.py` stamps for the published tap. #3861's
# first fix drifted by being applied to one of a pair, so both are pinned.
FORMULAS = {
    "api-local": REPO_ROOT / "homebrew" / "nyxgpt-api.rb",
    "api-tap-template": REPO_ROOT / "homebrew" / "tap" / "nyxgpt-api.rb.tmpl",
    "web-local": REPO_ROOT / "homebrew" / "nyxgpt-web.rb",
    "web-tap-template": REPO_ROOT / "homebrew" / "tap" / "nyxgpt-web.rb.tmpl",
}

WRAPPERS = {
    "api-local": '(bin/"nyxgpt-api").write <<~EOS',
    "api-tap-template": '(bin/"nyxgpt-api").write <<~EOS',
    "web-local": '(bin/"nyxgpt-web").write <<~SH',
    "web-tap-template": '(bin/"nyxgpt-web").write <<~SH',
}

READERS = {
    "api-local": ("(libexec/\"read-api-address.py\").write <<~'PY'", 2),
    "api-tap-template": ("(libexec/\"read-api-address.py\").write <<~'PY'", 2),
    "web-local": ("(libexec/\"read-web-config.py\").write <<~'PY'", 4),
    "web-tap-template": ("(libexec/\"read-web-config.py\").write <<~'PY'", 4),
}

# Homebrew's own test, verbatim from Library/Homebrew/utils/path.rb.
_TEXT_EXECUTABLE = re.compile(r"\A#!\s*\S+")

_HEREDOC_OPEN = re.compile(r"""\.write[( ]<<~'?(?P<term>[A-Z_]+)'?\)?\s*$""")


def _heredoc_body(text: str, opener: str) -> list[str]:
    """The raw source lines of the heredoc introduced by `opener`."""
    match = _HEREDOC_OPEN.search(opener)
    assert match is not None, f"not a squiggly-heredoc opener: {opener!r}"
    term = match.group("term")
    start = text.index(opener)
    body: list[str] = []
    for line in text[start:].split("\n")[1:]:
        if line.strip() == term:
            return body
        body.append(line)
    raise AssertionError(f"heredoc {term} is never terminated after {opener!r}")


def _render_squiggly(body: list[str]) -> str:
    """What Ruby's `<<~` writes for these source lines.

    Ruby removes the indentation of the least-indented line, ignoring lines
    that are entirely whitespace. Emulated rather than shelled out to so the
    guard runs on a box with no ruby; `test_the_emulation_matches_real_ruby`
    pins the emulation against the real interpreter where one exists.
    """
    indents = [len(line) - len(line.lstrip(" ")) for line in body if line.strip()]
    strip = min(indents) if indents else 0
    return "".join(line[strip:] + "\n" for line in body)


def _all_squiggly_openers(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if _HEREDOC_OPEN.search(line.strip())]


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_the_wrapper_shebang_lands_at_byte_zero(which):
    """The defect itself: an indented shebang is not a shebang."""
    text = FORMULAS[which].read_text(encoding="utf-8")
    rendered = _render_squiggly(_heredoc_body(text, WRAPPERS[which]))
    assert rendered.startswith("#!"), (
        f"{FORMULAS[which].name}'s wrapper renders as {rendered[:24]!r}. A `<<~` "
        "heredoc strips the indentation of its LEAST-indented line, so one "
        "column-0 line (a nested `<<'PY'` body is the way this happened) "
        "leaves the shebang indented -- see this module's docstring."
    )


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_homebrews_cleaner_would_keep_the_exec_bit(which):
    """The consequence: `Cleaner` re-stamps a non-shebang file 0444."""
    text = FORMULAS[which].read_text(encoding="utf-8")
    rendered = _render_squiggly(_heredoc_body(text, WRAPPERS[which]))
    assert _TEXT_EXECUTABLE.match(rendered[:1024]), (
        f"{FORMULAS[which].name}'s wrapper does not satisfy Homebrew's "
        "`Utils::Path.text_executable?`, so `Cleaner#clean_dir` will chmod it "
        "0444 and undo the formula's own `chmod 0755`"
    )


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_no_heredoc_in_these_formulas_is_de_indented_by_nothing(which):
    """The class, not the one spelling.

    Any `write <<~` whose first line is not among the least-indented is being
    rendered with indentation its author did not write, whether it is a shell
    script, a Python file or an ini file.
    """
    text = FORMULAS[which].read_text(encoding="utf-8")
    openers = _all_squiggly_openers(text)
    assert openers, f"no `write <<~` heredoc found in {FORMULAS[which].name}"
    for opener in openers:
        body = _heredoc_body(text, opener)
        nonblank = [line for line in body if line.strip()]
        assert nonblank, f"{opener} writes an empty file"
        indents = [len(line) - len(line.lstrip(" ")) for line in nonblank]
        first = len(body[0]) - len(body[0].lstrip(" "))
        assert first == min(indents), (
            f"{FORMULAS[which].name}: {opener} has a line indented less than its "
            f"first line (min={min(indents)}, first={first}), so `<<~` strips "
            "less than the source suggests. A nested heredoc body, which has to "
            "start at column 0, is how #4043 happened."
        )


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_the_wrapper_no_longer_nests_a_heredoc(which):
    """The wrapper reads its config from a file, so nothing forces column 0."""
    text = FORMULAS[which].read_text(encoding="utf-8")
    rendered = _render_squiggly(_heredoc_body(text, WRAPPERS[which]))
    assert "<<'PY'" not in rendered
    reader = "read-api-address.py" if which.startswith("api") else "read-web-config.py"
    assert '"$SYS_PY" "' in rendered and reader in rendered, (
        f"{FORMULAS[which].name}'s wrapper no longer calls its config reader; "
        "the host/port lookup has to come from somewhere"
    )


def _reader_script(which: str, tmp_path: Path) -> Path:
    text = FORMULAS[which].read_text(encoding="utf-8")
    opener, _fields = READERS[which]
    script = tmp_path / f"{which}-reader.py"
    script.write_text(_render_squiggly(_heredoc_body(text, opener)), encoding="utf-8")
    return script


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_the_config_reader_is_valid_python(which, tmp_path):
    source = _reader_script(which, tmp_path).read_text(encoding="utf-8")
    ast.parse(source)
    # It runs under /usr/bin/python3 on a Mac that may have no nyxGPT venv at
    # all, so the standard library is the whole budget.
    imported = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
    }
    assert imported <= {"configparser", "os"}, f"{which} reader imports {imported}"


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_the_config_reader_reports_the_configured_address(which, tmp_path):
    """Rendered and RUN: the wrapper's whole job is these fields."""
    script = _reader_script(which, tmp_path)
    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    (home / ".nyxGPT" / "config.ini").write_text(
        "[api]\nhost = 0.0.0.0\nport = 8123\n"
        "[web]\nhost = 127.0.0.5\nport = 3222\n"
        "api_base_url = http://api.example:8123\n"
        "[auth]\nenabled = true\napi_key =   secret-key\n",
        encoding="utf-8",
    )
    cp = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert cp.returncode == 0, cp.stderr
    fields = cp.stdout.rstrip("\n").split("\t")
    _opener, expected = READERS[which]
    assert len(fields) == expected, f"{which} printed {fields!r}"
    if which.startswith("api"):
        assert fields == ["0.0.0.0", "8123"]
    else:
        # The auth key is stripped, and only passed through when [auth] is on.
        assert fields == ["127.0.0.5", "3222", "http://api.example:8123", "secret-key"]


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_the_config_reader_falls_back_with_no_config_at_all(which, tmp_path):
    script = _reader_script(which, tmp_path)
    home = tmp_path / "empty-home"
    home.mkdir()
    cp = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert cp.returncode == 0, cp.stderr
    fields = cp.stdout.rstrip("\n").split("\t")
    if which.startswith("api"):
        assert fields == ["127.0.0.1", "8000"]
    else:
        assert fields == ["127.0.0.1", "3000", "", ""]


@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_the_guard_fails_on_the_defect_it_exists_to_catch(which):
    """Injection: a guard that cannot fail is not evidence (D-040).

    Puts the pre-fix shape back -- one column-0 line inside the wrapper's
    heredoc -- and requires both assertions to reject it.
    """
    text = FORMULAS[which].read_text(encoding="utf-8")
    body = _heredoc_body(text, WRAPPERS[which])
    injected = body[:1] + ["import configparser"] + body[1:]
    rendered = _render_squiggly(injected)
    assert not rendered.startswith("#!")
    assert not _TEXT_EXECUTABLE.match(rendered[:1024])
    indents = [len(line) - len(line.lstrip(" ")) for line in injected if line.strip()]
    first = len(injected[0]) - len(injected[0].lstrip(" "))
    assert first != min(indents)


@pytest.mark.skipif(shutil.which("ruby") is None, reason="no ruby on this runner")
@pytest.mark.parametrize("which", sorted(FORMULAS))
def test_the_emulation_matches_real_ruby(which, tmp_path):
    """`_render_squiggly` is only evidence if Ruby agrees with it."""
    text = FORMULAS[which].read_text(encoding="utf-8")
    body = _heredoc_body(text, WRAPPERS[which])
    out = tmp_path / "rendered"
    # Interpolation is irrelevant to the indentation question, so the two names
    # the wrappers interpolate are bound to literals and the heredoc is
    # reproduced verbatim.
    script = tmp_path / "render.rb"
    term = _HEREDOC_OPEN.search(WRAPPERS[which]).group("term")  # type: ignore[union-attr]
    script.write_text(
        'venv = "/K/libexec/venv"\n'
        'libexec = "/K/libexec"\n'
        "class StubFormula\n"
        "  def self.[](_name); self; end\n"
        '  def self.opt_bin; "/node/bin"; end\n'
        '  def self.opt_libexec; "/node/libexec"; end\n'
        "end\n"
        "Formula = StubFormula\n"
        f"out = <<~{term}\n" + "\n".join(body) + f"\n    {term}\n"
        f"File.write({str(out)!r}, out)\n",
        encoding="utf-8",
    )
    cp = subprocess.run(["ruby", str(script)], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    real = out.read_text(encoding="utf-8")
    # Ruby's own answer to the question this module is about.
    assert real.startswith("#!")
    assert _TEXT_EXECUTABLE.match(real[:1024])

    # And the emulation agrees about every line's indentation. The bytes
    # themselves differ -- Ruby expands `#{libexec}` and turns `\t` into a tab
    # -- neither of which bears on where the indentation ends up.
    def indents(text: str) -> list[int]:
        return [len(line) - len(line.lstrip(" ")) for line in text.split("\n") if line.strip()]

    assert indents(real) == indents(_render_squiggly(body))
