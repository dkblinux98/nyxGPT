"""The agentic first principles bind from one source, and bite at review (#3821).

The principles (owner requirement 2026-08-16, ledger D-012) are stated in full
in exactly one file: `CLAUDE.md` § Agentic First Principles. `claude-code-action`
loads that file into every agent run as project instructions (ledger V-028), so
the text binds at runtime without being copied anywhere -- and copying it into a
prompt would violate the principles it installs: paid for on every run forever
(principle 1) and free to drift out of step with the source, so a later edit
updates some copies and not others (principle 2).

Prose is the only mechanism here, so its absence -- or its duplication -- is the
regression. These tests pin three things:

  * the single source exists and stays single;
  * the agent-facing surfaces (workflow prompts, prompt files, runbooks,
    charters) cite it rather than restating it;
  * principles 4 and 2 exist as blocking review findings, defined once in
    `agents/runbooks/review-runbook.md` §1d/§1e and cited by the review prompts.

Repetition is not what makes doctrine bind (ledger D-011): a check at the review
checkpoint costs nothing per run, catches a violation whether or not the agent
read the principle, and lives in one file.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
LEDGER = REPO_ROOT / "agents" / "LEDGER.md"
REVIEW_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "review-runbook.md"
REVIEW_PROMPT = REPO_ROOT / "agents" / "prompts" / "review-agent.prompt.md"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-code-review.yml"
CANARY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-md-binding-canary.yml"

# Sentences a verbatim copy of the principles would carry. Matched
# case-sensitively and in full: the failure mode being detected is a paste, and
# an exact sentence is what a paste contains. A gloss that names a principle in
# passing ("the fourth principle forbids ...") is the intended form and must not
# trip this.
PRINCIPLE_SENTENCES = (
    "Consider cost.",
    "Consider future harm",
    "Never take change action without first seeking to understand.",
)

# A file carrying this many of the sentences above is restating the principles,
# not citing them. One sentence is a quotation; two is a copy.
COPY_THRESHOLD = 2

# Only these two may carry the principles' own words: CLAUDE.md is the source,
# AGENTS.md is the index into the agent system and carries the summary the
# owner blessed in #3821. Everything else cites.
PRINCIPLE_TEXT_ALLOWED = {CLAUDE_MD, AGENTS_MD}

# Surfaces an agent reads at runtime -- exactly where a copy would be paid for
# on every run, and exactly where the first draft of #3821 proposed putting one.
AGENT_SURFACE_GLOBS = (
    (".github/workflows", "*.yml"),
    ("agents/prompts", "*.md"),
    ("agents/runbooks", "*.md"),
    ("agents/charters", "*.md"),
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing contract file: {path}"
    return path.read_text(encoding="utf-8")


def _agent_surface_files() -> list[Path]:
    files: list[Path] = []
    for directory, pattern in AGENT_SURFACE_GLOBS:
        root = REPO_ROOT / directory
        assert root.is_dir(), f"missing agent surface directory: {root}"
        files.extend(sorted(root.glob(pattern)))
    assert files, "no agent surface files found -- the scan would pass vacuously"
    return files


def _tracked_markdown_files() -> list[Path]:
    """Every `*.md` file the repository actually contains.

    Deliberately `git ls-files` and not `rglob`: the claim under test is about
    the *repository*, and a checkout is not the same thing as a working
    directory. Agent runners drop untracked copies of `CLAUDE.md` beside the
    checkout (`.claude-pr/CLAUDE.md` is written by the review workflow), which
    an unfiltered walk read as a second committed statement of the principles
    -- so the test failed on a workspace artifact, in the review gate, on
    whatever PR happened to be under review. A committed duplicate anywhere in
    the tree still fails; a scratch file no longer can.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files = [REPO_ROOT / name for name in result.stdout.split("\0") if name]
    assert files, "no tracked markdown files found -- the scan would pass vacuously"
    return files


def _copied_sentence_count(text: str) -> int:
    return sum(1 for sentence in PRINCIPLE_SENTENCES if sentence in text)


@pytest.fixture(scope="module")
def claude_md() -> str:
    return _read(CLAUDE_MD)


@pytest.fixture(scope="module")
def review_runbook() -> str:
    return _read(REVIEW_RUNBOOK)


def test_claude_md_states_the_principles_in_full(claude_md: str) -> None:
    assert "## Agentic First Principles" in claude_md, (
        "CLAUDE.md must carry the § Agentic First Principles section -- it is "
        "the single source the whole design of #3821 rests on"
    )
    section = claude_md.split("## Agentic First Principles", 1)[1]
    section = section.split("\n---\n", 1)[0]
    for sentence in PRINCIPLE_SENTENCES:
        assert sentence in section, f"CLAUDE.md is missing principle text: {sentence!r}"


def test_the_source_stays_single(claude_md: str) -> None:
    """The long-form statement exists in CLAUDE.md and nowhere else in the tree."""
    marker = "Where these conflict with speed, they win."
    assert marker in claude_md, (
        "CLAUDE.md must keep the full statement of the principles; this test "
        "uses its closing sentence as the marker for 'the full statement'"
    )

    duplicates = [
        path.relative_to(REPO_ROOT)
        for path in sorted(_tracked_markdown_files())
        if path != CLAUDE_MD and marker in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not duplicates, (
        "the principles are stated in full outside CLAUDE.md: "
        f"{duplicates}. A second copy is paid for on every run and drifts out "
        "of step with the source -- cite CLAUDE.md § Agentic First Principles "
        "instead (#3821)"
    )


def test_an_untracked_workspace_copy_is_not_a_duplicate(claude_md: str) -> None:
    """The scan reads the checkout, not whatever the runner left lying beside it.

    Injects the exact artifact that made this contract fail in the review gate:
    `.claude-pr/CLAUDE.md`, an untracked copy of the source. Before the scan was
    narrowed to tracked files this reported a duplicate and failed every review
    round run in such a workspace, on PRs that had touched nothing.
    """
    artifact_dir = REPO_ROOT / ".claude-pr"
    artifact = artifact_dir / "CLAUDE.md"

    # The review gate creates this artifact itself, so on the runner it is
    # usually already here. Refusing to run in that case would fail the test in
    # the one environment the fix is about; instead, assert the property against
    # the real artifact and never delete what this test did not create.
    dir_preexisted = artifact_dir.exists()
    file_preexisted = artifact.exists()

    if not dir_preexisted:
        artifact_dir.mkdir()
    try:
        if not file_preexisted:
            artifact.write_text(claude_md, encoding="utf-8")
        assert artifact.exists(), "the injected artifact is missing -- nothing was tested"
        assert artifact not in _tracked_markdown_files()
        test_the_source_stays_single(claude_md)
    finally:
        if not file_preexisted and artifact.exists():
            artifact.unlink()
        if not dir_preexisted and artifact_dir.exists():
            shutil.rmtree(artifact_dir)


def test_agent_surfaces_cite_rather_than_restate() -> None:
    """No prompt, runbook, charter or workflow gains a copy of the principles."""
    offenders = {
        str(path.relative_to(REPO_ROOT)): _copied_sentence_count(
            path.read_text(encoding="utf-8", errors="ignore")
        )
        for path in _agent_surface_files()
        if path not in PRINCIPLE_TEXT_ALLOWED
        and _copied_sentence_count(path.read_text(encoding="utf-8", errors="ignore"))
        >= COPY_THRESHOLD
    }
    assert not offenders, (
        f"agent surfaces restate the first principles instead of citing them: "
        f"{offenders}. CLAUDE.md is loaded into every agent run as project "
        "instructions (ledger V-028), so a copy buys nothing and costs tokens "
        "on every run forever -- cite `CLAUDE.md § Agentic First Principles`"
    )


def test_review_runbook_defines_the_diagnosis_gate(review_runbook: str) -> None:
    assert "## 1d) Diagnosis gate" in review_runbook, (
        "review-runbook.md must define principle 4 as a review finding -- the "
        "half that would have caught #3788 (#3821)"
    )
    section = review_runbook.split("## 1d)", 1)[1].split("## 1e)", 1)[0]
    flowed = " ".join(section.split())

    assert "Medium (blocking)" in flowed, "the diagnosis gate must block, not advise"
    assert "CLAUDE.md" in flowed, "the diagnosis gate must cite the single source"
    # The gate is about the cause, not about the patch's shape.
    assert "never established" in flowed, (
        "the diagnosis gate must name its blocking condition: a fix aimed at a "
        "cause that was never established"
    )
    # Symmetry: an unbounded gate gets ignored or turns into a tax (§2).
    assert "Symmetry" in section, (
        "the diagnosis gate must state its own limits, as §1a and §1c do -- "
        "feature work has no cause to establish"
    )


def test_review_runbook_defines_the_generality_gate(review_runbook: str) -> None:
    assert "## 1e) Generality gate" in review_runbook, (
        "review-runbook.md must define principle 2 as a review finding: a "
        "narrow patch on a general defect blocks (#3821)"
    )
    section = review_runbook.split("## 1e)", 1)[1].split("## 2)", 1)[0]
    flowed = " ".join(section.split())

    assert "Medium (blocking)" in flowed, "the generality gate must block, not advise"
    assert "CLAUDE.md" in flowed, "the generality gate must cite the single source"
    assert "#3500" in flowed and "#3816" in flowed, (
        "the generality gate must keep its 'why': the project-hygiene clobber "
        "was fixed once for a single author while every other author kept racing"
    )
    assert "Symmetry" in section, (
        "the generality gate must state its own limits -- it asks for the "
        "sweep and its result, not for every fix to become a refactor"
    )


@pytest.mark.parametrize("path", [REVIEW_PROMPT, REVIEW_WORKFLOW])
def test_review_prompts_cite_both_gates(path: Path) -> None:
    """A finding the review agent is never told to make is not enforced (D-011)."""
    text = _read(path)
    for section in ("1d", "1e"):
        assert f"§{section}" in text or f"review-runbook {section}" in text, (
            f"{path.relative_to(REPO_ROOT)} must cite review-runbook §{section} "
            "the way it already cites §1a and §1c -- the runbook defines the "
            "finding, the prompt is what makes the agent look for it"
        )
    # ...and cite it, not copy the principles into the prompt.
    assert _copied_sentence_count(text) < COPY_THRESHOLD, (
        f"{path.relative_to(REPO_ROOT)} restates the principles; cite "
        "`CLAUDE.md § Agentic First Principles` instead (#3821)"
    )


def test_binding_canary_proves_both_halves() -> None:
    """The canary must be able to fail, or it verifies nothing (D-006)."""
    text = _read(CANARY_WORKFLOW)
    assert "--setting-sources user" in text, (
        "the canary needs a control run that excludes the `project` setting "
        "source; without it, a positive result cannot distinguish 'CLAUDE.md "
        "was loaded' from 'the model read the file' or 'the assertion is vacuous'"
    )
    assert "workflow_dispatch" in text, (
        "the canary is dispatch-only on purpose: every run spends real money "
        "and the fact it checks changes only when the action changes"
    )
    assert "V-028" in text, "the canary must point at the ledger entry it maintains"


class TestCanaryAssertion:
    """Run the canary's verdict step, so the workflow does not ship unexecuted.

    The canary itself costs two model calls and is dispatch-only, so it is not
    run on every PR. Its *assertion* is the part that decides what a run means,
    and it is ordinary bash: extracted from the real workflow YAML and executed
    here against synthetic outputs, exactly as
    `scripts/agents/lib/escalation_script_probe.py` does for the escalation
    step (**V-027**). A verdict that cannot fail would turn the canary into a
    green light that means nothing.
    """

    STEP_NAME = "Assert both halves"
    TOKEN = "nyxgpt-1234-1-deadbeefdeadbeef"

    @staticmethod
    def _assert_step_body() -> str:
        workflow = yaml.safe_load(CANARY_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["canary"]["steps"]
        for step in steps:
            if step.get("name") == TestCanaryAssertion.STEP_NAME:
                return str(step["run"])
        raise AssertionError(
            f"{CANARY_WORKFLOW.name} has no {TestCanaryAssertion.STEP_NAME!r} step; "
            "this test extracts the real body so a rename cannot silently skip it"
        )

    def _run(self, tmp_path: Path, expected: str, default: str, control: str):
        script = tmp_path / "assert.sh"
        script.write_text(self._assert_step_body(), encoding="utf-8")
        return subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "EXPECTED": expected,
                "DEFAULT_OUT": default,
                "CONTROL_OUT": control,
            },
        )

    @staticmethod
    def _out(canary: str) -> str:
        return json.dumps({"canary": canary, "instructions_loaded": canary != "NOT-LOADED"})

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:  # pragma: no cover - present on CI runners
            pytest.skip("jq is required to execute the canary's assertion body")

    def test_passes_when_default_loads_and_control_does_not(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, self.TOKEN, self._out(self.TOKEN), self._out("NOT-LOADED"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CLAUDE.md IS loaded" in result.stdout

    def test_fails_when_the_default_run_does_not_return_the_token(self, tmp_path: Path) -> None:
        """The finding the canary exists to make: V-028 has gone stale."""
        result = self._run(tmp_path, self.TOKEN, self._out("NOT-LOADED"), self._out("NOT-LOADED"))
        assert result.returncode != 0
        assert "NOT loaded" in result.stdout + result.stderr

    def test_fails_when_the_control_run_also_returns_the_token(self, tmp_path: Path) -> None:
        """Token arriving by some path other than project instructions."""
        result = self._run(tmp_path, self.TOKEN, self._out(self.TOKEN), self._out(self.TOKEN))
        assert result.returncode != 0
        assert "control run returned the token" in result.stdout + result.stderr

    @pytest.mark.parametrize("empty", ["", "null"])
    def test_fails_when_a_run_produced_no_structured_output(
        self, tmp_path: Path, empty: str
    ) -> None:
        """A model call that died must not read as a verdict."""
        missing_default = self._run(tmp_path, self.TOKEN, empty, self._out("NOT-LOADED"))
        assert missing_default.returncode != 0

        missing_control = self._run(tmp_path, self.TOKEN, self._out(self.TOKEN), empty)
        assert missing_control.returncode != 0


def test_ledger_records_the_verification_and_its_qualifications() -> None:
    ledger = _read(LEDGER)
    entry = ledger.split("- **V-028**", 1)
    assert len(entry) == 2, "ledger must carry V-028, the CLAUDE.md loading verification"
    body = " ".join(entry[1].split("\n- **", 1)[0].split())

    assert "Method:" in body, "V-028 needs a repeatable method (ledger entry schema)"
    assert "Re-verify when:" in body, "V-028 needs its staleness condition"
    # The PR-context caveat is the part a future session is most likely to get
    # wrong: a PR editing CLAUDE.md does not bind its own review.
    assert "base branch" in body, (
        "V-028 must record that on PR runs the action restores CLAUDE.md from "
        "the PR's base branch -- a PR's own edit does not bind its review"
    )
