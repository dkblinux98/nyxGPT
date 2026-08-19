"""The Homebrew install has to name `nyxgpt up`, and be checked for it (#3854).

Why this file exists. All four formulas declare a `service` block, and none of
them declared `caveats`. Homebrew fills that silence with its own generated
message -- "To start nyxgpt-api now and restart at login: brew services start
nyxgpt-api" -- which was therefore the only instruction an operator ever
received. The owner followed it. `brew services start` starts the one service
it names and never reaches `ops.install()`, so the machine came up with no
Ollama, no Cassandra, no observability and no packaged resources under
`~/.nyxGPT`, and the web UI answered HTTP 500 on the session list. The
requirement that observability runs on every install path was implemented and
simultaneously unreachable by anyone reading the install's own output.

The real assertion is on a real `brew install` transcript and needs Homebrew,
so it lives in `scripts/homebrew-caveats-smoke.sh`, driven by the keg-install
job of `macos-brew-smoke.yml`. Two things about that arrangement can rot in
ways a macOS runner would not catch, and those are what this file pins:

  * the guidance itself, in each of the four formulas -- including the web
    keg's, which the macOS job only installs when `include_web` is set, so on
    an ordinary PR this is the only thing checking it;
  * the checker's discrimination, by running it here (Linux, no Homebrew, via
    `--check-only`) over a transcript that carries the guidance and over the
    pre-fix transcript that does not. A checker that passes on both is the
    defect it was written to prevent, wearing a green tick.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "homebrew-caveats-smoke.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "macos-brew-smoke.yml"

FORMULAS = (
    REPO_ROOT / "homebrew" / "nyxgpt-api.rb",
    REPO_ROOT / "homebrew" / "nyxgpt-web.rb",
    REPO_ROOT / "homebrew" / "tap" / "nyxgpt-api.rb.tmpl",
    REPO_ROOT / "homebrew" / "tap" / "nyxgpt-web.rb.tmpl",
)

# The caveats method, from `def caveats` to the first line that is exactly
# `  end`. The heredoc body is indented further, so nothing inside it can close
# the match early.
_CAVEATS_RE = re.compile(r"\n  def caveats\n(?P<body>.*?)\n  end\n", re.S)

# What Homebrew prints for a `service` formula that declares no caveats -- the
# entire instruction the owner was given, reproduced here as the negative
# fixture. Nothing in it names `nyxgpt up`.
PRE_FIX_TRANSCRIPT = """\
==> Fetching nyxgpt/brew-smoke/nyxgpt-api@3.0.0rc
==> Installing nyxgpt-api@3.0.0rc from nyxgpt/brew-smoke
==> Caveats
To start nyxgpt-api@3.0.0rc now and restart at login:
  brew services start nyxgpt-api@3.0.0rc
==> Summary
🍺  /opt/homebrew/Cellar/nyxgpt-api@3.0.0rc/3.0.0rc0: 1,234 files, 456MB
"""


def _caveats_body(formula: Path) -> str:
    match = _CAVEATS_RE.search(formula.read_text(encoding="utf-8"))
    assert match is not None, (
        f"{formula.relative_to(REPO_ROOT)} declares no `caveats` block, so "
        "Homebrew's generated `brew services start` line is again the only "
        "instruction the operator gets (#3854)"
    )
    return match.group("body")


def _rendered_caveats(formula: Path, name: str) -> str:
    """The caveats text as Homebrew would print it for formula `name`.

    Only the interpolations the formulas actually use are substituted; a new
    one would surface here as a leftover `#{...}` and fail the render check
    below rather than silently reaching an operator.
    """
    body = _caveats_body(formula)
    text = body.split("<<~EOS\n", 1)[1].rsplit("    EOS", 1)[0]
    api_formula = name.replace("nyxgpt-web", "nyxgpt-api")
    return (
        text.replace("#{name}", name).replace("#{api_formula}", api_formula)
        # The teardown guidance (#3859) names the tap to untap; Homebrew fills
        # it from the loaded formula, and the `|| "<your-tap>"` arm is what a
        # formula loaded from a file rather than a tap prints.
        .replace('#{tap || "<your-tap>"}', "nyxgpt/brew-smoke")
    )


def _script_env_name(formula: Path) -> str:
    """The formula name an rc-channel install of this file would carry."""
    stem = "nyxgpt-api" if "api" in formula.name else "nyxgpt-web"
    return f"{stem}@3.0.0rc"


@pytest.mark.parametrize("formula", FORMULAS, ids=lambda p: p.name)
def test_every_formula_declares_caveats(formula: Path) -> None:
    """All four, not just the two the local install path uses.

    `homebrew/*.rb` is what `nyxgpt ops install` stamps into a local tap and
    `homebrew/tap/*.rb.tmpl` is what publishes to the remote one. The owner
    installed from the remote tap, so a fix that reached only the pair a
    developer sees would not have changed what they were told.
    """
    assert _caveats_body(formula)


@pytest.mark.parametrize("formula", FORMULAS, ids=lambda p: p.name)
def test_every_formula_declares_exactly_one_caveats_block(formula: Path) -> None:
    """Two `def caveats` in one formula is one message, not two.

    Ruby keeps the last definition of a method and discards the earlier one
    silently -- `ruby -c` is clean, `brew audit` says nothing, and the formula
    installs. The only symptom is that half the guidance never reaches the
    operator. This is not hypothetical: merging v3.0.0 into this branch put the
    teardown block (#3859) and the startup block (#3854) in every formula, and
    git resolved it without a conflict because neither side's lines overlapped.
    `scripts/homebrew-caveats-smoke.sh` also requires exactly one block to
    inject its negative control, but that check only runs on macOS -- this one
    runs on every PR.
    """
    text = formula.read_text(encoding="utf-8")
    assert text.count("  def caveats\n") == 1, (
        f"{formula.relative_to(REPO_ROOT)} declares "
        f"{text.count('  def caveats')} caveats blocks; Ruby will print only "
        "the last, so whichever guidance came first is silently lost. Merge "
        "them into one block instead of adding a second."
    )


@pytest.mark.parametrize("formula", FORMULAS, ids=lambda p: p.name)
def test_caveats_name_nyxgpt_up_and_what_brew_services_omits(formula: Path) -> None:
    """Naming the right command is half of it; the other half is the warning.

    An operator handed `brew services start` by Homebrew and `nyxgpt up` by the
    formula has been given two commands and no way to choose. The text has to
    say which one starts the stack and what the other one leaves out, or the
    original failure is still reachable by picking the wrong one.
    """
    rendered = _rendered_caveats(formula, _script_env_name(formula))
    assert "nyxgpt up" in rendered
    assert "brew services start" in rendered
    lowered = rendered.lower()
    for subsystem in ("ollama", "cassandra", "observability"):
        assert subsystem in lowered, (
            f"{formula.relative_to(REPO_ROOT)} does not say that `brew services "
            f"start` leaves {subsystem} out -- the operator has no reason to "
            "prefer `nyxgpt up` (#3854)"
        )


@pytest.mark.parametrize("formula", FORMULAS, ids=lambda p: p.name)
def test_caveats_render_for_both_channels(formula: Path) -> None:
    """The rc channel installs the same recipe under a different formula name.

    `nyxgpt-api@3.0.0rc` is what acceptance testing installs, so a caveats
    block that hard-coded `nyxgpt-api` would print a `brew services` command
    for a formula the machine does not have, and -- in the web formula -- a
    `brew install nyxgpt-api` that pulls the stable keg alongside a candidate
    it conflicts with. Interpolation off `name` is what avoids that, so assert
    the rendered text carries the channel it was rendered for and no leftover
    interpolation.
    """
    for name in (_script_env_name(formula), formula.name.split(".")[0]):
        rendered = _rendered_caveats(formula, name)
        assert name in rendered
        assert "#{" not in rendered, "an un-substituted interpolation would reach the operator"


def test_web_caveats_point_at_the_formula_that_ships_the_cli() -> None:
    """`nyxgpt` lives in the api keg, so the web keg cannot assume it is there.

    `homebrew/nyxgpt-api.rb` installs it (`bin.install_symlink venv/bin/nyxgpt`)
    and neither web formula does. Telling a web-only machine to run `nyxgpt up`
    without saying where the command comes from repeats #3850's shape: correct
    instruction, absent command.
    """
    for formula in (
        REPO_ROOT / "homebrew" / "nyxgpt-web.rb",
        REPO_ROOT / "homebrew" / "tap" / "nyxgpt-web.rb.tmpl",
    ):
        rendered = _rendered_caveats(formula, "nyxgpt-web@3.0.0rc")
        assert "brew install nyxgpt-api@3.0.0rc" in rendered


def _run_checker(tmp_path: Path, transcript: str) -> subprocess.CompletedProcess:
    target = tmp_path / "transcript.log"
    target.write_text(transcript, encoding="utf-8")
    return subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--check-only", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_checker_passes_on_a_transcript_carrying_the_guidance(tmp_path: Path) -> None:
    transcript = (
        "==> Installing nyxgpt-api@3.0.0rc from nyxgpt/brew-smoke\n"
        "==> Caveats\n"
        + _rendered_caveats(REPO_ROOT / "homebrew" / "nyxgpt-api.rb", "nyxgpt-api@3.0.0rc")
        + "==> Summary\n"
    )
    result = _run_checker(tmp_path, transcript)
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_fails_on_the_transcript_the_owner_actually_saw(tmp_path: Path) -> None:
    """The whole point of the gate: it has to fail on the pre-fix output.

    A checker green on both transcripts would have let #3854 ship again with a
    passing macOS job attached to it.
    """
    result = _run_checker(tmp_path, PRE_FIX_TRANSCRIPT)
    assert result.returncode == 1
    assert "nyxgpt up" in result.stdout


def test_checker_ignores_matches_outside_the_caveats_section(tmp_path: Path) -> None:
    """`brew install --verbose` prints thousands of lines before the caveats.

    Vendored source paths alone mention ollama, cassandra and observability, so
    a checker grepping the whole transcript would have passed on the very
    install that produced the defect. Narrowing to the `==> Caveats` section is
    what makes the assertion about what the operator was told.
    """
    noisy = (
        "==> Fetching nyxgpt-api@3.0.0rc\n"
        "  installing src/nyxgpt/ollama_client.py\n"
        "  installing src/nyxgpt/cassandra_store.py\n"
        "  installing src/nyxgpt/observability.py\n"
        "  nyxgpt up\n" + PRE_FIX_TRANSCRIPT
    )
    result = _run_checker(tmp_path, noisy)
    assert result.returncode == 1, result.stdout + result.stderr


def test_workflow_runs_the_checker_against_the_captured_install() -> None:
    """Asserting on a transcript needs the transcript kept.

    The install step used to pipe straight into `tail`, so the caveats -- which
    are printed once, at install time -- were gone by the time any later step
    could look. If the `tee` goes, the checker still passes on a file that no
    longer exists only because the step would fail outright, so pin both the
    capture and the call.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["keg-install"]["steps"]
    run_blocks = "\n".join(step.get("run", "") for step in steps)

    assert 'tee "$RUNNER_TEMP/install-nyxgpt-api.log"' in run_blocks
    assert "scripts/homebrew-caveats-smoke.sh" in run_blocks
    assert "$RUNNER_TEMP/install-nyxgpt-api.log" in run_blocks

    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "scripts/homebrew-caveats-smoke.sh" in triggers["pull_request"]["paths"], (
        "the checker is run by a PR-triggered job, so a change to it has to be "
        "able to trigger that job"
    )
