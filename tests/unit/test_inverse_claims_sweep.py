"""The mechanized inverse-claims sweep (#4015).

`tests/unit/test_inverse_claims_contract.py` pins the *rule* -- that the review
runbook, the review prompt and the developer runbook all carry the
inverse-claims criterion and that an unfixed falsified claim blocks. This file
pins the *tool* that makes the developer half of that rule cheap enough to
actually happen, and the properties that decide whether it gets used:

- it finds prose that makes a CLAIM about what the diff changed, not prose that
  merely mentions it;
- it groups those places by the fact they assert, because #3811 showed the real
  problem is one fact written in ten files, not ten forgotten files;
- it never judges truth and never fails on a hit;
- the noise controls that were tuned against the 2026-08-22 round stay in
  place, because every one of them was added to kill a specific false positive
  that had reached the top of a checklist.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "agents" / "inverse_claims_sweep.py"
DEVELOPER_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "developer-runbook.md"
SUBMIT_SCRIPT = REPO_ROOT / "scripts" / "agents" / "developer_submit_for_review.sh"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("inverse_claims_sweep", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so a module
    # loaded by path must be registered before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sweep_mod() -> ModuleType:
    return _load()


# ---------------------------------------------------------------- extraction


def test_a_dotted_reference_yields_its_symbol(sweep_mod: ModuleType) -> None:
    """`ops._native_service_version` must extract `_native_service_version`.

    The first cut excluded a preceding `.`, which silently dropped every
    attribute-style reference -- and with it the #3850 finding, whose stale
    workaround names `nyxgpt.ops._native_service_version`.
    """
    found = dict(sweep_mod._terms_in_line("uses nyxgpt.ops._native_service_version"))
    assert found.get("function") == "_native_service_version"


def test_commands_come_from_code_spans_not_running_prose(sweep_mod: ModuleType) -> None:
    """`nyxgpt ops status` again once ...` must not become a four-word command."""
    terms = sweep_mod._terms_in_line("run `nyxgpt ops status` again once the service is up")
    commands = [text for kind, text in terms if kind == "command"]
    assert commands == ["nyxgpt ops status"]


def test_flag_and_value_are_one_term(sweep_mod: ModuleType) -> None:
    terms = sweep_mod._terms_in_line("`nyxgpt cloud deploy --os macos` refuses")
    assert ("flag-value", "--os macos") in terms
    assert ("command", "nyxgpt cloud deploy --os macos") in terms


@pytest.mark.parametrize(
    "text",
    ["/bin/bash", "/opt/homebrew/bin/nyxgpt", "/Users/someone/x/y"],
)
def test_filesystem_paths_are_not_endpoints(sweep_mod: ModuleType, text: str) -> None:
    """These have the shape of a route and none of the meaning."""
    assert not sweep_mod._plausible("endpoint", text)


@pytest.mark.parametrize("text", ["side_effect", "return_value", "RuntimeError", "--force"])
def test_language_furniture_is_not_a_topic(sweep_mod: ModuleType, text: str) -> None:
    assert text in sweep_mod.TERM_STOPWORDS


def test_a_bare_filename_cannot_anchor_a_hit(sweep_mod: ModuleType) -> None:
    """`main.tf` is named by half the tree; it may corroborate, never anchor.

    Measured on #3995's diff before this rule: 201 of 282 checklist rows rested
    on nothing but terms of this shape.
    """
    assert not sweep_mod.Term(text="main.tf", kind="path").anchor
    assert sweep_mod.Term(text="terraform/aws/mac/main.tf", kind="path").anchor
    assert sweep_mod.Term(text="nyxgpt ops status", kind="command").anchor


def test_a_retired_path_is_the_strongest_signal(sweep_mod: ModuleType) -> None:
    retired = sweep_mod.Term(text="web/src/components/SupportTicketDialog.tsx", kind="retired-path")
    assert retired.weight >= sweep_mod.Term(text="nyxgpt ops status", kind="command").weight


# --------------------------------------------------------------- prose blocks


def test_python_prose_is_parsed_not_pattern_matched(sweep_mod: ModuleType) -> None:
    """A module-level string constant must not be mistaken for a docstring.

    The line-scanning first cut read the closing `\"\"\"` of a `SCRIPT = \"\"\"...`
    constant as a docstring opener and swallowed the code after it, which put
    `capture_output` at the top of a checklist.
    """
    source = "\n".join(
        [
            '"""Module doc: this must never be skipped."""',
            "",
            'SCRIPT = """',
            "planted = []",
            "assert capture_output is not None",
            '"""',
            "",
            "def f():",
            '    """Real docstring."""',
            "    return 1",
        ]
    )
    texts = [b.text for b in sweep_mod._python_blocks("x.py", source)]
    assert any("must never be skipped" in t for t in texts)
    assert any("Real docstring" in t for t in texts)
    assert not any("planted" in t for t in texts)


def test_markdown_tables_are_not_prose(sweep_mod: ModuleType) -> None:
    block = sweep_mod.Block(
        "docs/api.md", 1, 3, "| Endpoint | Method |\n|---|---|\n| `/health` | GET |"
    )
    assert sweep_mod._is_tabular(block)


def test_hash_colon_comments_lose_the_colon(sweep_mod: ModuleType) -> None:
    blocks = sweep_mod._prefixed_comment_blocks("x.py", "#: The bounds mirror support's.\n", "#")
    assert blocks[0].text == "The bounds mirror support's."


# ------------------------------------------------------------- claim filtering


@pytest.mark.parametrize(
    "sentence",
    [
        "It is what the dashboard renders and what `nyxgpt ops status` prints, "
        "so the three cannot disagree.",
        "nyxGPT deliberately does not allocate one: `nyxgpt cloud deploy --os macos` refuses.",
        "The rc tarball vendors `pyproject.toml` verbatim, so without this the keg lies.",
        "A repo checkout is still required for now.",
    ],
)
def test_claims_are_recognised(sweep_mod: ModuleType, sentence: str) -> None:
    assert any(rx.search(sentence) for rx in sweep_mod._CLAIM_RE.values())


def test_a_bare_pointer_is_not_a_claim(sweep_mod: ModuleType) -> None:
    """A mention survives most changes; that is why mentions are filtered out."""
    sentence = "See `nyxgpt ops status` for the report format."
    assert not any(rx.search(sentence) for rx in sweep_mod._CLAIM_RE.values())


def test_the_claim_window_spans_the_neighbouring_sentence(sweep_mod: ModuleType) -> None:
    """The symbol and the claim about it are often adjacent sentences.

    `test_macos_user_path_smoke.py`'s docstring names
    `ops._native_service_version` in one sentence and asserts "vendors
    `pyproject.toml` verbatim" in the next; a strict per-sentence test misses
    the #3850 finding entirely.
    """
    text = (
        "`ops._native_service_version` reads the installed metadata to pick a formula. "
        "The rc tarball vendors `pyproject.toml` verbatim, so an unstamped checkout lies."
    )
    window = sweep_mod._claim_window(text, text.index("_native_service_version"))
    assert "verbatim" in window


# ------------------------------------------------------------------ clustering


def _place(mod: ModuleType, path: str, line: int, sentence: str) -> object:
    block = mod.Block(path, line, line, sentence)
    return mod.Hit(block=block, sentence=sentence, line=line, terms=[], claim_kinds=["negation"])


def test_a_cluster_reports_density_not_a_list_of_strangers(sweep_mod: ModuleType) -> None:
    """The unit of the report is one fact, however many places assert it.

    This is the owner's 2026-08-22 amendment: "this fact lives in 7 files" and
    "check these 7 unrelated places" are different messages, and only the first
    tells us whether the tree needs a deduplication pass.
    """
    cluster = sweep_mod.Cluster(
        term=sweep_mod.Term(text="submit_ticket", kind="function"),
        places=[
            _place(sweep_mod, "docs/ui.md", 10, "The filer is not asked for a version."),
            _place(sweep_mod, "src/nyxgpt/support.py", 20, "submit_ticket refuses an empty title."),
        ],
    )
    assert cluster.density == 2
    assert cluster.audiences == ["python code", "user docs"]


def test_audience_specific_restatement_is_distinguished_from_copy_paste(
    sweep_mod: ModuleType,
) -> None:
    """A guess for a human, and it says which evidence it used.

    The tool cannot reliably tell legitimate audience-specific prose from an
    accidental paste, and is not asked to -- it must surface the distinction so
    a person decides (#4015 amendment).
    """
    term = sweep_mod.Term(text="submit_ticket", kind="function")
    pasted = sweep_mod.Cluster(
        term=term,
        places=[
            _place(sweep_mod, "docs/a.md", 1, "The filer answers three questions in the chat."),
            _place(sweep_mod, "docs/b.md", 1, "The filer answers three questions in the chat."),
        ],
    )
    restated = sweep_mod.Cluster(
        term=term,
        places=[
            _place(sweep_mod, "docs/ui.md", 1, "Nothing else writes to the Support surface."),
            _place(
                sweep_mod,
                "src/nyxgpt/support.py",
                1,
                "An oversized body is refused before it ever reaches GitHub.",
            ),
        ],
    )
    assert pasted.shape == "copy-paste"
    assert restated.shape == "restatement"
    assert "audience" in restated.shape_note


def test_one_near_identical_pair_does_not_make_a_wide_cluster_copy_paste(
    sweep_mod: ModuleType,
) -> None:
    """Two of thirty sentences rhyming is not a copy-paste problem."""
    term = sweep_mod.Term(text="nyxgpt ops status", kind="command")
    varied = [
        "Kubernetes mode never dials the host daemon for this.",
        "A missing kubeconfig must not read as an unreachable cluster.",
        "The deployment block is printed before anything asks the cluster.",
        "Only an install marker gates the doctor probe, so a fresh machine pays nothing.",
        "An unreadable cluster reports cannot-determine rather than missing.",
        "No transport detail may travel out with the error class.",
        "Remediation names the kubernetes install command instead of the native one.",
        "The pod summary counts ready replicas, not scheduled ones.",
    ]
    places = [_place(sweep_mod, f"docs/{n}.md", 1, sentence) for n, sentence in enumerate(varied)]
    places += [
        _place(sweep_mod, "docs/x.md", 1, "The same readiness view is not recomputed."),
        _place(sweep_mod, "docs/y.md", 1, "The same readiness view is not recomputed."),
    ]
    assert sweep_mod.Cluster(term=term, places=places).shape != "copy-paste"


def test_places_are_spread_across_files_before_they_are_truncated(
    sweep_mod: ModuleType,
) -> None:
    """A truncated cluster must show breadth, or the finding falls below the fold.

    `docs/api.md`'s falsified sentence (#3987) sat 22nd by raw score in a
    39-place cluster; ordering by best-per-file is what brings it up.
    """
    places = [
        _place(sweep_mod, "docs/ops.md", 1, "not a"),
        _place(sweep_mod, "docs/ops.md", 2, "not b"),
        _place(sweep_mod, "docs/api.md", 3, "not c"),
    ]
    ordered = sweep_mod._spread_by_file(places)
    paths = [p.block.path for p in ordered]
    assert set(paths[:2]) == {"docs/ops.md", "docs/api.md"}, "both files before either repeats"
    assert paths[2] == "docs/ops.md"


# ------------------------------------------------------------------ contract


def test_the_sweep_never_fails_on_a_hit() -> None:
    """Advisory by construction: exit 0 whenever the sweep ran.

    A tool that cries wolf gets ignored; a tool that *blocks* on a wolf it
    cannot identify gets disabled. The only non-zero exit is the sweep itself
    being unable to run.
    """
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), "--base", "HEAD~1", "--head", "HEAD", "--max-items", "3"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "CHECKLIST, not a gate" in proc.stdout or "No prose in the tree" in proc.stdout


def test_a_broken_range_reports_instead_of_pretending() -> None:
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), "--base", "no/such/ref", "--head", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "could not run" in proc.stderr


def test_the_developer_runbook_points_at_the_script() -> None:
    """The rule without the tool is what got skipped; §5 must name the tool."""
    section = DEVELOPER_RUNBOOK.read_text(encoding="utf-8").split("## 5) Documentation", 1)[1]
    section = " ".join(section.split("## 6)", 1)[0].split())
    assert "scripts/agents/inverse_claims_sweep.py" in section
    assert "never judges truth and never fails on a hit" in section
    assert "#4015" in section
    assert "density" in section, "the runbook must tell the author to read the density"


def test_the_submit_script_runs_the_sweep_and_cannot_be_blocked_by_it() -> None:
    """Run at submission so a skipped sweep is visible, never so it can block."""
    text = SUBMIT_SCRIPT.read_text(encoding="utf-8")
    assert "inverse_claims_sweep.py" in text
    assert "#4015" in text
    block = text.split("Inverse-claims sweep", 1)[1].split("---- Record a CI override", 1)[0]
    assert "It is ADVISORY" in block
    assert "2>/dev/null" in block, "a sweep failure must not surface as a submission failure"
