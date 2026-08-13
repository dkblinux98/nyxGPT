"""Unit tests for scripts/build_homebrew_artifacts.py (#3622).

Runs the release script end-to-end against the real repo checkout (it
needs real pyproject.toml/src/nyxgpt/web trees to vendor -- same
precondition `_create_dist_tarball` already has) and asserts the stamped
formulas are well-formed and placeholder-free.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_homebrew_artifacts.py"
BASE_URL = "https://github.com/dkblinux98/nyxGPT/releases/download/9.9.9"

_spec = importlib.util.spec_from_file_location("build_homebrew_artifacts", SCRIPT)
assert _spec is not None and _spec.loader is not None
build_homebrew_artifacts = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = build_homebrew_artifacts
_spec.loader.exec_module(build_homebrew_artifacts)


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("homebrew-artifacts")
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9", str(out_dir), BASE_URL],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert cp.returncode == 0, f"script failed:\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    return out_dir


@pytest.fixture(scope="module")
def built_rc_artifacts(tmp_path_factory):
    """The rc channel's output: the `@<release>rc` formulas, nothing else."""
    out_dir = tmp_path_factory.mktemp("homebrew-artifacts-rc")
    cp = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "9.9.9rc4",
            str(out_dir),
            "https://github.com/dkblinux98/nyxGPT/releases/download/9.9.9rc4",
            "--channel",
            "rc",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert cp.returncode == 0, f"script failed:\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    return out_dir


@pytest.mark.parametrize("argv", [["9.9.9"], ["9.9.9", "out", "url", "--channel"]])
def test_usage_error_on_wrong_arg_count(argv):
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert cp.returncode == 2
    assert "usage:" in cp.stderr


def test_usage_error_on_unknown_channel(tmp_path):
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9", str(tmp_path), "https://x", "--channel", "nightly"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert cp.returncode == 2
    assert "usage:" in cp.stderr


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_tarball_built_and_sha256_matches_formula(built_artifacts, name):
    tarball = built_artifacts / "dist" / f"{name}-9.9.9.tar.gz"
    assert tarball.is_file()
    assert tarfile.is_tarfile(tarball)

    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()

    formula = (built_artifacts / f"{name}.rb").read_text(encoding="utf-8")
    assert f'sha256 "{digest}"' in formula
    assert f'url "{BASE_URL}/{name}-9.9.9.tar.gz"' in formula
    assert 'version "9.9.9"' in formula
    assert "__URL__" not in formula
    assert "__SHA256__" not in formula
    assert "__VERSION__" not in formula


def test_api_tarball_contains_vendored_source(built_artifacts):
    tarball = built_artifacts / "dist" / "nyxgpt-api-9.9.9.tar.gz"
    with tarfile.open(tarball) as tf:
        names = tf.getnames()
    assert "nyxgpt-api-9.9.9/pyproject.toml" in names
    assert "nyxgpt-api-9.9.9/example.config.ini" in names
    assert any(n.startswith("nyxgpt-api-9.9.9/src/nyxgpt/") for n in names)


def test_web_tarball_excludes_node_modules(built_artifacts):
    tarball = built_artifacts / "dist" / "nyxgpt-web-9.9.9.tar.gz"
    with tarfile.open(tarball) as tf:
        names = tf.getnames()
    assert any(n.endswith("package.json") for n in names)
    assert not any("node_modules" in n for n in names)


def test_stable_channel_writes_only_the_stable_formulas(built_artifacts):
    written = sorted(p.name for p in built_artifacts.glob("*.rb"))

    assert written == ["nyxgpt-api.rb", "nyxgpt-web.rb"]


# --- The rc channel (#3727) ----------------------------------------------


def test_rc_channel_never_writes_a_stable_formula(built_rc_artifacts):
    """Clobber-safety, at the only place it can be guaranteed: the file names.

    Homebrew has no pre-release semantics, so `brew install nyxgpt-api`
    resolving to the latest stable release depends entirely on an rc publish
    not producing a `nyxgpt-api.rb` at all.
    """
    written = sorted(p.name for p in built_rc_artifacts.glob("*.rb"))

    assert written == ["nyxgpt-api@9.9.9rc.rb", "nyxgpt-web@9.9.9rc.rb"]
    assert not (built_rc_artifacts / "nyxgpt-api.rb").exists()
    assert not (built_rc_artifacts / "nyxgpt-web.rb").exists()


@pytest.mark.parametrize(
    ("name", "class_name"),
    [("nyxgpt-api", "NyxgptApiAT999rc"), ("nyxgpt-web", "NyxgptWebAT999rc")],
)
def test_rc_formula_declares_homebrews_class_name(built_rc_artifacts, name, class_name):
    """`brew` derives the class from the file name -- a mismatch fails to load."""
    formula = (built_rc_artifacts / f"{name}@9.9.9rc.rb").read_text(encoding="utf-8")

    assert f"class {class_name} < Formula" in formula


@pytest.mark.parametrize(
    ("formula", "class_name"),
    [
        # Verified against real Homebrew: `brew ruby -e 'puts
        # Formulary.class_s("<formula>")'`.
        # The names the rc channel actually stamps since #3735: the release
        # line lives in the formula name, and the digit after `@` is what
        # makes brew translate it to `AT`.
        ("nyxgpt-api@3.0.0rc", "NyxgptApiAT300rc"),
        ("nyxgpt-web@3.0.0rc", "NyxgptWebAT300rc"),
        ("nyxgpt-api", "NyxgptApi"),
        ("python@3.12", "PythonAT312"),
        # The trap this pins: `@` is translated to `AT` only before a DIGIT,
        # so a `@rc` suffix survives into a constant Ruby cannot declare.
        ("nyxgpt-api@rc", "NyxgptApi@rc"),
    ],
)
def test_formula_class_name_follows_homebrews_rule(formula, class_name):
    assert build_homebrew_artifacts.formula_class_name(formula) == class_name


@pytest.mark.parametrize("formula", ["nyxgpt-api@rc", "nyxgpt-web@candidate", "foo@beta"])
def test_unloadable_formula_names_are_refused(formula):
    """A `@<non-digit>` formula cannot be loaded by any Homebrew -- fail at build.

    `brew` looks for the constant `Formulary.class_s` derives from the *file
    name*; for `nyxgpt-api@rc` that is `NyxgptApi@rc`, which is not legal
    Ruby, so no `class ... < Formula` line inside the file can satisfy the
    loader ("Expected to find class NyxgptApi@rc"). This is why the rc
    channel's suffix names the release line -- `@3.0.0rc`, whose digit makes
    brew translate the `@` -- rather than a bare `@rc`.
    """
    with pytest.raises(ValueError, match="not a loadable Homebrew formula name"):
        build_homebrew_artifacts.assert_loadable_formula_name(formula)


@pytest.mark.parametrize("channel", ["stable", "rc"])
@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_every_published_formula_name_is_loadable(name, channel):
    formula = build_homebrew_artifacts.formula_name(name, channel, "3.0.0rc7")

    assert build_homebrew_artifacts.assert_loadable_formula_name(formula) == formula
    # The constant brew will look for has to be declarable Ruby.
    assert build_homebrew_artifacts.formula_class_name(formula).isidentifier()


def test_the_names_this_script_stamps_are_the_names_the_cli_tells_users_to_install():
    """One source of truth: the tap job stamps what `nyxgpt release publish` prints.

    `release_candidate.rc_formulas` is what the CLI, the dashboard panel and
    the docs render as the `brew install` line -- a rename here that missed
    it would advertise a formula the tap does not carry.
    """
    from nyxgpt.release_candidate import rc_formulas

    stamped = tuple(
        build_homebrew_artifacts.formula_name(name, "rc", "3.0.0rc7")
        for name in ("nyxgpt-api", "nyxgpt-web")
    )

    assert stamped == rc_formulas("3.0.0")
    assert stamped == ("nyxgpt-api@3.0.0rc", "nyxgpt-web@3.0.0rc")


def test_the_rc_formula_name_carries_the_release_line():
    """The property the owner's naming exists for (#3735): a candidate for one
    line can never silently become a candidate for the next -- they are
    differently named formulas, so crossing lines takes a deliberate install."""
    api = build_homebrew_artifacts.formula_name

    assert api("nyxgpt-api", "rc", "3.0.0rc4") == "nyxgpt-api@3.0.0rc"
    assert api("nyxgpt-api", "rc", "3.1.0rc1") == "nyxgpt-api@3.1.0rc"
    assert api("nyxgpt-api", "rc", "3.0.0rc4") != api("nyxgpt-api", "rc", "3.1.0rc1")
    # Stable is unsuffixed whatever version it stamps -- that is the formula
    # `brew install nyxgpt-api` resolves.
    assert api("nyxgpt-api", "stable", "3.0.0") == "nyxgpt-api"


def test_an_rc_formula_cannot_be_named_without_a_version():
    """Naming it anyway would produce a line-less `@rc` brew cannot even load."""
    with pytest.raises(ValueError, match="needs the candidate's version"):
        build_homebrew_artifacts.formula_name("nyxgpt-api", "rc")


@pytest.mark.parametrize("version", ["3.0.0rc4", "3.0.0"])
def test_release_line_accepts_a_release_or_a_candidate_of_one(version):
    assert build_homebrew_artifacts.release_line(version) == "3.0.0"


@pytest.mark.parametrize("version", ["rc4", "3.0", "3.0.0.dev1", "latest"])
def test_release_line_refuses_a_version_it_cannot_place(version):
    with pytest.raises(ValueError, match="not a release or release-candidate version"):
        build_homebrew_artifacts.release_line(version)


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_rc_formula_conflicts_with_its_stable_counterpart(built_rc_artifacts, name):
    formula = (built_rc_artifacts / f"{name}@9.9.9rc.rb").read_text(encoding="utf-8")

    assert f'conflicts_with "{name}",' in formula
    assert "because:" in formula


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_rc_formula_is_stamped_with_the_real_rc_tarball(built_rc_artifacts, name):
    tarball = built_rc_artifacts / "dist" / f"{name}-9.9.9rc4.tar.gz"
    assert tarfile.is_tarfile(tarball)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()

    formula = (built_rc_artifacts / f"{name}@9.9.9rc.rb").read_text(encoding="utf-8")

    assert f'sha256 "{digest}"' in formula
    assert 'version "9.9.9rc4"' in formula
    assert (
        f'url "https://github.com/dkblinux98/nyxGPT/releases/download/9.9.9rc4/'
        f'{name}-9.9.9rc4.tar.gz"' in formula
    )
    assert "__URL__" not in formula
    assert "__SHA256__" not in formula
    assert "__VERSION__" not in formula


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_rc_formula_installs_exactly_what_the_stable_one_does(
    built_artifacts, built_rc_artifacts, name
):
    """Derived from the same template, not a parallel copy: only the identity differs."""
    stable = (built_artifacts / f"{name}.rb").read_text(encoding="utf-8")
    rc = (built_rc_artifacts / f"{name}@9.9.9rc.rb").read_text(encoding="utf-8")

    # Everything from `def install` down is byte-identical -- the keg an RC
    # installs is the same recipe as the release's, on a different tarball.
    assert stable[stable.index("  def install") :] == rc[rc.index("  def install") :]


# --- Backfilling a tag that predates the tooling (#3737) ------------------
#
# 2.1.0 was cut before any of this existed: its tree has no
# `scripts/build_homebrew_artifacts.py` and no `homebrew/tap/*.rb.tmpl`, so
# the tap job's single "checkout the tag" step could not publish it at all.
# The split these tests pin is tooling-here / source-there: the templates and
# the builder come from the checkout the script runs in, the tarball contents
# come from `--source-root`.


def _write_fake_source_tree(root: Path) -> Path:
    """A minimal but complete nyxGPT source checkout, marked so it is identifiable.

    Stands in for a checkout of an old release tag: same layout, contents
    that could not possibly have come from this working tree.
    """
    (root / "pyproject.toml").write_text(
        '[project]\nname = "nyxgpt-from-the-tag"\n', encoding="utf-8"
    )
    (root / "example.config.ini").write_text("[nyxgpt]\n; from the tag\n", encoding="utf-8")
    (root / "src" / "nyxgpt").mkdir(parents=True)
    (root / "src" / "nyxgpt" / "__init__.py").write_text(
        '__version__ = "from-the-tag"\n', encoding="utf-8"
    )
    (root / "web").mkdir()
    (root / "web" / "package.json").write_text(
        '{"name": "nyxgpt-web-from-the-tag"}\n', encoding="utf-8"
    )
    return root


@pytest.fixture(scope="module")
def backfilled_artifacts(tmp_path_factory):
    """Stable formulas for a version whose source is a *different* checkout."""
    source_root = _write_fake_source_tree(tmp_path_factory.mktemp("release-source"))
    out_dir = tmp_path_factory.mktemp("homebrew-artifacts-backfill")
    cp = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "9.9.9",
            str(out_dir),
            BASE_URL,
            "--source-root",
            str(source_root),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert cp.returncode == 0, f"script failed:\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    return out_dir


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_backfill_vendors_the_tarballs_from_the_source_root(backfilled_artifacts, name):
    """What users install has to be the *tag's* code, not the tooling branch's."""
    with tarfile.open(backfilled_artifacts / "dist" / f"{name}-9.9.9.tar.gz") as tf:
        names = tf.getnames()
        if name == "nyxgpt-api":
            member = tf.extractfile("nyxgpt-api-9.9.9/pyproject.toml")
        else:
            member = tf.extractfile("nyxgpt-web-9.9.9/package.json")
        assert member is not None
        content = member.read().decode("utf-8")

    assert "from-the-tag" in content
    # ...and nothing leaked in from this checkout alongside it.
    if name == "nyxgpt-api":
        assert sorted(n for n in names if n.endswith(".py")) == [
            "nyxgpt-api-9.9.9/src/nyxgpt/__init__.py"
        ]


def test_backfill_stamps_formulas_from_the_tooling_checkouts_templates(backfilled_artifacts):
    """The whole point: the tag has no templates, so these must be ours.

    Placeholder-free and pointing at the real tarball -- a formula stamped
    from the tag's (nonexistent) templates could not exist at all.
    """
    for name in ("nyxgpt-api", "nyxgpt-web"):
        formula = (backfilled_artifacts / f"{name}.rb").read_text(encoding="utf-8")
        digest = hashlib.sha256(
            (backfilled_artifacts / "dist" / f"{name}-9.9.9.tar.gz").read_bytes()
        ).hexdigest()

        template = (REPO_ROOT / "homebrew" / "tap" / f"{name}.rb.tmpl").read_text(encoding="utf-8")
        assert formula == (
            template.replace("__URL__", f"{BASE_URL}/{name}-9.9.9.tar.gz")
            .replace("__SHA256__", digest)
            .replace("__VERSION__", "9.9.9")
        )


def test_backfill_writes_only_the_stable_formulas(backfilled_artifacts):
    """Guardrail: a backfill never touches an `@<line>rc` formula."""
    assert sorted(p.name for p in backfilled_artifacts.glob("*.rb")) == [
        "nyxgpt-api.rb",
        "nyxgpt-web.rb",
    ]


def test_default_source_root_is_this_checkout():
    assert build_homebrew_artifacts.resolve_source_root(None) is None


def test_source_root_must_be_a_real_source_checkout(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not a nyxGPT source checkout"):
        build_homebrew_artifacts.resolve_source_root(tmp_path)


def test_source_root_must_exist(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        build_homebrew_artifacts.resolve_source_root(tmp_path / "nope")


def test_script_reports_a_bad_source_root_without_a_traceback(tmp_path):
    """An operator error in a release run should read as one line, not a crash."""
    out_dir = tmp_path / "out"
    cp = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "9.9.9",
            str(out_dir),
            BASE_URL,
            "--source-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert cp.returncode == 2
    assert "not a nyxGPT source checkout" in cp.stderr
    assert "Traceback" not in cp.stderr


def test_source_root_flag_without_a_value_is_a_usage_error(tmp_path):
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9", str(tmp_path), BASE_URL, "--source-root"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert cp.returncode == 2
    assert "usage:" in cp.stderr


# --- The stable channel only ever stamps a real release (#3737) -----------


@pytest.mark.parametrize("version", ["2.1.0", "3.0.0", "10.2.13"])
def test_assert_release_version_accepts_a_release(version):
    assert build_homebrew_artifacts.assert_release_version(version) == version


@pytest.mark.parametrize("version", ["3.0.0rc4", "3.0.0.dev1", "v2.1.0", "latest", "2.1"])
def test_stable_formulas_are_refused_for_anything_but_a_release(version):
    """`brew install nyxgpt-api` must never be able to land on a pre-release."""
    with pytest.raises(ValueError, match="not a release version"):
        build_homebrew_artifacts.assert_release_version(version)


def test_stable_build_refuses_a_release_candidate_version(tmp_path):
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9rc4", str(tmp_path / "out"), BASE_URL],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert cp.returncode == 2
    assert "not a release version" in cp.stderr
    # Refused before anything was built, not cleaned up afterwards.
    assert not list((tmp_path / "out").glob("*.rb"))


# --- The tap job's backfill path (#3737) ----------------------------------


def _release_artifacts_workflow() -> dict:
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / "release-artifacts.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _tap_job() -> dict:
    return _release_artifacts_workflow()["jobs"]["homebrew-tap"]


def test_tap_job_checks_out_the_tooling_and_the_tag_separately():
    """The 2026-08-11 failure: one checkout of the tag, no build script in it."""
    checkouts = [s for s in _tap_job()["steps"] if "actions/checkout" in str(s.get("uses", ""))]

    assert len(checkouts) == 2
    tooling, source = checkouts
    # The tooling checkout takes no `ref`: the ref the run started on is the
    # release commit on a `released` event, and the dispatched branch (which
    # has the tooling) on a backfill.
    assert "ref" not in tooling.get("with", {})
    assert source["with"]["ref"] == "${{ env.VERSION }}"
    assert source["with"]["path"] == "release-source"


def test_tap_job_builds_the_tarballs_from_the_tag_source():
    run_steps = "\n".join(step.get("run", "") for step in _tap_job()["steps"])

    assert "scripts/build_homebrew_artifacts.py" in run_steps
    assert "--source-root release-source" in run_steps


def test_tap_job_verifies_the_tag_is_a_published_release():
    """Dispatch accepts any string; the stable formulas accept only a release."""
    steps = _tap_job()["steps"]
    run_steps = "\n".join(step.get("run", "") for step in steps)

    assert any("published stable release" in str(s.get("name", "")) for s in steps)
    assert "gh release view" in run_steps
    assert "isPrerelease" in run_steps
    assert "isDraft" in run_steps


def test_tap_job_asserts_it_stamped_no_rc_formula():
    steps = _tap_job()["steps"]
    run_steps = "\n".join(step.get("run", "") for step in steps)

    assert any("only the stable formulas" in str(s.get("name", "")) for s in steps)
    assert '"nyxgpt-api.rb nyxgpt-web.rb "' in run_steps


def test_backfill_is_a_parameter_of_the_existing_job_not_a_second_workflow():
    """Owner criterion: a dispatchable one-shot, not a parallel copy."""
    workflow = _release_artifacts_workflow()
    jobs = workflow["jobs"]
    inputs = workflow[True]["workflow_dispatch"]["inputs"]

    assert inputs["tap_only"]["type"] == "boolean"
    assert inputs["tap_only"]["default"] is False
    # `tap_only` skips everything except the tap job -- which is never gated
    # by it, or a backfill would publish nothing.
    assert "if" not in jobs["homebrew-tap"]
    for job in ("container-images", "artifact-install-smoke", "ec2-linux-user-data-smoke"):
        assert jobs[job]["if"] == "${{ !inputs.tap_only }}"


# --- The script's dependency surface (#3741) ------------------------------
#
# The first live rc cut published 3.0.0rc1 to PyPI and then lost the tap:
# `homebrew-tap-rc` checks the repo out, sets Python up and runs this script
# with no `pip install` step at all, and the script imported `nyxgpt.ops` --
# which imports httpx (and pynacl, and the metrics/tracing stack) at module
# level. `ModuleNotFoundError: No module named 'httpx'`, after the candidate
# was already immutable on PyPI.
#
# The fix was the import boundary rather than a longer install step (the
# builder now lives in the stdlib-only `nyxgpt.release_tarball`), so what
# these tests pin is the *pairing*: whatever the script imports, the jobs
# that run it must provide. Both halves are checked -- what the script needs,
# and what the jobs install -- so either side moving alone fails here instead
# of mid-release.


def _third_party_imports() -> list[str]:
    """Top-level non-stdlib packages loading this script pulls in.

    `nyxgpt` itself doesn't count: the jobs check the repo out, and the
    script puts `src/` on `sys.path` -- but anything else has to come from
    an install step the tap jobs don't have.
    """
    probe = f"""
import importlib.util, json, sys
baseline = set(sys.modules)
spec = importlib.util.spec_from_file_location("bha", {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
sys.modules["bha"] = module
spec.loader.exec_module(module)
roots = {{name.split(".")[0] for name in set(sys.modules) - baseline}}
print(json.dumps(sorted(
    root for root in roots
    if root not in sys.stdlib_module_names
    and not root.startswith("_")
    and root not in ("nyxgpt", "bha")
)))
"""
    cp = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, f"probe failed:\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    return list(json.loads(cp.stdout))


def test_build_script_imports_nothing_beyond_the_stdlib():
    """#3741: what the tap jobs give this script is a checkout and a Python."""
    assert _third_party_imports() == []


def _job_install_commands(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def _jobs_running_the_build_script() -> list[tuple[str, str, dict]]:
    """Every (workflow, job name, job) in .github/workflows that runs the script."""
    import yaml

    found = []
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            steps = job.get("steps") or []
            runs = "\n".join(str(step.get("run", "")) for step in steps)
            if "scripts/build_homebrew_artifacts.py" in runs:
                found.append((path.name, job_name, job))
    return found


def test_both_tap_jobs_are_still_the_only_callers():
    """A third caller needs the same dependency check applied to it."""
    callers = {(workflow, job) for workflow, job, _ in _jobs_running_the_build_script()}

    assert callers == {
        ("release-artifacts.yml", "homebrew-tap"),
        ("release-publish-pypi.yml", "homebrew-tap-rc"),
    }


def test_jobs_running_the_build_script_provide_what_it_imports():
    """The #3741 pairing: the job's setup must cover the script's imports.

    Today the script needs nothing installed, so a bare checkout plus
    `setup-python` is a complete setup and neither job has an install step.
    Add a third-party import anywhere in the script's closure without adding
    an install step to both jobs and this fails -- which is the failure the
    rc cut discovered by publishing to PyPI first.
    """
    needed = _third_party_imports()

    for workflow, job_name, job in _jobs_running_the_build_script():
        installs = _job_install_commands(job)
        missing = [package for package in needed if package not in installs]
        assert not missing, (
            f"{workflow}:{job_name} runs scripts/build_homebrew_artifacts.py but "
            f"never installs {', '.join(missing)} -- either add an install step "
            "to every job that runs it, or keep the script's imports inside the "
            "stdlib-only nyxgpt.release_tarball boundary (#3741)"
        )
        # The script vendors `src/nyxgpt/` and reaches `nyxgpt` through it, so
        # a checkout is the one thing the setup can never drop.
        assert any(
            "actions/checkout" in str(step.get("uses", "")) for step in job["steps"]
        ), f"{workflow}:{job_name} runs the build script without checking the repo out"


def test_build_script_runs_end_to_end_with_third_party_imports_blocked(tmp_path):
    """The CI condition itself: a Python with no site-packages worth having.

    Blocking rather than trusting the dev venv is the point -- every
    developer machine has httpx installed, so nothing short of blocking it
    reproduces what the tap jobs actually run with.
    """
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        '''"""Refuse every import that isn't stdlib or nyxGPT (test hook)."""
import sys


class _ThirdPartyBlocker:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in sys.stdlib_module_names or root in ("nyxgpt", "sitecustomize"):
            return None
        raise ModuleNotFoundError(f"blocked third-party import: {fullname}")


sys.meta_path.insert(0, _ThirdPartyBlocker())
''',
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9rc1", str(out_dir), BASE_URL, "--channel", "rc"],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONPATH": str(hook_dir)},
    )

    assert cp.returncode == 0, f"script failed:\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    assert "ModuleNotFoundError" not in cp.stderr
    assert (out_dir / "nyxgpt-api@9.9.9rc.rb").is_file()
    assert (out_dir / "nyxgpt-web@9.9.9rc.rb").is_file()
    assert (out_dir / "dist" / "nyxgpt-api-9.9.9rc1.tar.gz").is_file()
    assert (out_dir / "dist" / "nyxgpt-web-9.9.9rc1.tar.gz").is_file()


def test_the_blocker_hook_would_have_caught_the_original_failure(tmp_path):
    """Guard the guard: a blocked import must actually fail the run."""
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        "import sys\n\n\n"
        "class _Blocker:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] == 'httpx':\n"
        "            raise ModuleNotFoundError('blocked third-party import: httpx')\n"
        "        return None\n\n\n"
        "sys.meta_path.insert(0, _Blocker())\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [sys.executable, "-c", "import httpx"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(hook_dir)},
    )

    assert cp.returncode != 0
    assert "blocked third-party import: httpx" in cp.stderr
