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
import re
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


# --- Immutable releases: the tarballs get their own release (#3763) -------
#
# Both 2.1.0 backfill runs built the tarballs correctly and then died on
# `HTTP 422: Cannot upload assets to an immutable release`, trying to
# `gh release upload --clobber` them onto the already-published 2.1.0
# release. A published release here can never gain an asset -- and that is
# not a backfill-only problem: the ceremony publishes every release before
# this job builds its tarballs, so the upload step could never have worked
# for any version. The tarballs are published on a release of their own,
# `<version>-homebrew`, created with the assets attached.


def test_asset_release_tag_is_the_versions_own_sidecar():
    assert build_homebrew_artifacts.asset_release_tag("2.1.0") == "2.1.0-homebrew"


def test_asset_release_tag_refuses_anything_but_a_release():
    """A candidate's tarballs ride its own prerelease -- it has no sidecar."""
    with pytest.raises(ValueError):
        build_homebrew_artifacts.asset_release_tag("9.9.9rc4")


def test_asset_release_tag_refuses_a_tag_it_produced_itself():
    """`2.1.0-homebrew-homebrew` would be a release nothing publishes."""
    from nyxgpt import release_tarball

    with pytest.raises(ValueError):
        release_tarball.homebrew_asset_release_tag("2.1.0-homebrew")


def test_asset_tag_query_prints_the_serving_release():
    """What the tap job builds BASE_URL from -- one definition, not a shell string."""
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "--asset-tag", "2.1.0"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "2.1.0-homebrew"


@pytest.mark.parametrize("argv", [["--asset-tag"], ["--asset-tag", "9.9.9", "out"]])
def test_asset_tag_query_takes_exactly_one_version(argv):
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert cp.returncode == 2
    assert "usage:" in cp.stderr


def test_asset_tag_query_reports_a_non_release_version_without_a_traceback():
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "--asset-tag", "9.9.9rc4"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert cp.returncode == 2
    assert "Traceback" not in cp.stderr
    assert "not a release version" in cp.stderr


def _tap_run_steps() -> str:
    return "\n".join(step.get("run", "") for step in _tap_job()["steps"])


def test_tap_job_never_uploads_assets_onto_an_existing_release():
    """The literal 2026-08-13 failure: `gh release upload --clobber` on 2.1.0."""
    run_steps = _tap_run_steps()

    assert "gh release upload" not in run_steps
    assert "--clobber" not in run_steps


def test_tap_job_publishes_the_tarballs_with_the_release_that_carries_them():
    run_steps = _tap_run_steps()

    assert "gh release create" in run_steps
    # Assets as positional arguments of the create call -- the only way an
    # immutable release can ever carry them.
    assert '"$API_TARBALL" "$WEB_TARBALL"' in run_steps
    # Never a second release of nyxGPT, and never able to re-trigger this
    # workflow (which listens for `released`, not `prereleased`).
    assert "--prerelease" in run_steps
    assert "--latest=false" in run_steps


def test_tap_job_stamps_the_formulas_against_the_serving_release():
    """A formula stamped with the version's own tag would 404 at `brew install`."""
    run_steps = _tap_run_steps()

    assert 'ASSET_TAG=$(python scripts/build_homebrew_artifacts.py --asset-tag "$VERSION")' in (
        run_steps
    )
    assert "releases/download/${ASSET_TAG}" in run_steps
    # ...and nothing points the formulas at the version's own release.
    assert "releases/download/${VERSION}" not in run_steps


def test_tap_job_reads_the_serving_release_back_before_pushing_the_formulas():
    steps = _tap_job()["steps"]
    names = [str(step.get("name", "")) for step in steps]

    verify = next(i for i, name in enumerate(names) if "Verify the serving release" in name)
    push = next(i for i, name in enumerate(names) if "Push stamped formulas" in name)
    assert verify < push
    assert "does not carry" in steps[verify]["run"]


# --- Formulas must agree with the bytes that are actually served (#3763) ---
#
# Tarball builds are not byte-reproducible: gzip embeds a build timestamp and
# the tar members carry the checkout's mtimes, so two builds of identical
# source differ in sha256 (demonstrated on #3769: `77d28855...` vs
# `d3d8e39d...` seconds apart -- not asserted here, because two builds inside
# one second *can* coincide and a flaky proof of the premise is worth less
# than the invariants below). Any path that stamps a *fresh build* while an
# earlier run's assets stay published therefore ships formulas whose checksum
# brew can never satisfy -- and because releases are immutable, the served
# bytes can never be corrected to match. The reuse path stamps against the
# downloaded assets instead, and the verify step re-checks the digest.


def test_tap_job_stamps_the_formulas_against_the_already_published_tarballs():
    steps = _tap_job()["steps"]
    names = [str(step.get("name", "")) for step in steps]
    run_steps = _tap_run_steps()

    # The serving release's completeness is resolved *before* anything is
    # stamped, because it is what decides where the sha256 comes from.
    resolve = next(i for i, name in enumerate(names) if "Resolve the release" in name)
    download = next(i for i, name in enumerate(names) if "already carries" in name)
    build = next(i for i, name in enumerate(names) if "Build tarballs and stamp" in name)
    assert resolve < download < build
    assert steps[download]["if"] == "${{ env.SERVED == 'true' }}"
    assert "gh release download" in steps[download]["run"]
    assert "SERVED=${SERVED}" in steps[resolve]["run"]

    # Served bytes stamp the formulas; only an unpublished version is built.
    assert "--tarballs-from served-tarballs" in run_steps
    assert "--source-root release-source" in run_steps


def test_tap_job_does_not_read_an_api_failure_as_an_empty_release():
    """A suppressed 5xx would read a *serving* sidecar as empty and retire it."""
    resolve = next(
        s for s in _tap_job()["steps"] if "Resolve the release" in str(s.get("name", ""))
    )
    run = resolve["run"]

    # `gh release view` failing is only benign when the release is absent;
    # anything else fails the job rather than being answered with "".
    assert "2>/dev/null" not in run
    assert "grep -qi 'release not found'" in run
    assert "refusing to guess" in run


def test_tap_job_verifies_the_stamped_sha256_against_the_served_asset():
    """Without this, a mismatch reaches the tap and breaks every `brew install`."""
    steps = _tap_job()["steps"]
    verify = next(s for s in steps if "Verify the serving release" in str(s.get("name", "")))
    run = verify["run"]

    # The digest is checked against the downloaded asset, not against what
    # the run believes it published.
    assert "gh release download" in run
    assert "sha256sum" in run
    assert 'if [ "$served" != "$stamped" ]; then' in run
    assert "exit 1" in run


def test_tap_job_reuses_the_serving_release_without_republishing():
    publish = next(
        s for s in _tap_job()["steps"] if "Publish the tarballs" in str(s.get("name", ""))
    )

    # Reuse is the resolved decision, not a second look at the release: a
    # divergence between the two would stamp one way and publish the other.
    assert 'if [ "${SERVED}" = "true" ]; then' in publish["run"]
    assert "reusing it" in publish["run"]


def test_stamping_from_published_tarballs_uses_their_digest(tmp_path):
    """The reuse path: the formula's sha256 is the served file's, not a rebuild's."""
    served = tmp_path / "served"
    served.mkdir()
    digests = {}
    for name in ("nyxgpt-api", "nyxgpt-web"):
        tarball = served / f"{name}-9.9.9.tar.gz"
        tarball.write_bytes(b"not really a tarball, but these are the published bytes")
        digests[name] = hashlib.sha256(tarball.read_bytes()).hexdigest()

    out_dir = tmp_path / "out"
    build_homebrew_artifacts.build("9.9.9", out_dir, BASE_URL, tarballs_from=served)

    for name, digest in digests.items():
        assert f'sha256 "{digest}"' in (out_dir / f"{name}.rb").read_text(encoding="utf-8")
        # ...and the bytes land where a build would have put them, so the
        # publish step's existence guard and the uploaded artifact agree.
        assert (out_dir / "dist" / f"{name}-9.9.9.tar.gz").read_bytes() == (
            served / f"{name}-9.9.9.tar.gz"
        ).read_bytes()


@pytest.mark.parametrize(
    "present", [(), ("nyxgpt-api",), ("nyxgpt-web",)], ids=["neither", "api-only", "web-only"]
)
def test_stamping_refuses_a_half_published_tarball_directory(tmp_path, present):
    """Stamping half a pair would leave one formula disagreeing with its bytes."""
    served = tmp_path / "served"
    served.mkdir()
    for name in present:
        (served / f"{name}-9.9.9.tar.gz").write_bytes(b"published")

    with pytest.raises(ValueError, match="does not hold the published tarballs"):
        build_homebrew_artifacts.build("9.9.9", tmp_path / "out", BASE_URL, tarballs_from=served)


def test_stamping_refuses_an_empty_published_tarball(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    (served / "nyxgpt-api-9.9.9.tar.gz").write_bytes(b"published")
    (served / "nyxgpt-web-9.9.9.tar.gz").write_bytes(b"")

    with pytest.raises(ValueError, match="missing or empty"):
        build_homebrew_artifacts.build("9.9.9", tmp_path / "out", BASE_URL, tarballs_from=served)


def test_source_root_and_tarballs_from_are_mutually_exclusive(tmp_path):
    """Nothing is vendored when the published tarballs are what is stamped."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_homebrew_artifacts.build(
            "9.9.9",
            tmp_path / "out",
            BASE_URL,
            source_root=REPO_ROOT,
            tarballs_from=tmp_path,
        )


def test_tarballs_from_a_missing_directory_is_reported_without_a_traceback(tmp_path):
    cp = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "9.9.9",
            str(tmp_path / "out"),
            BASE_URL,
            "--tarballs-from",
            str(tmp_path / "nope"),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert cp.returncode == 2
    assert "Traceback" not in cp.stderr
    assert "does not exist" in cp.stderr


def test_tarballs_from_without_a_value_is_a_usage_error(tmp_path):
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9", str(tmp_path / "out"), BASE_URL, "--tarballs-from"],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert cp.returncode == 2
    assert "usage:" in cp.stderr


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


def test_the_known_callers_are_still_the_only_callers():
    """A new caller needs the same dependency check applied to it.

    The macOS smoke job (#3753) is the third: it stamps the working tree's
    formulas into a throwaway tap so a broken recipe fails on the PR that
    wrote it. It is held to the same bar as the two publishing jobs by
    `test_jobs_running_the_build_script_provide_what_it_imports` below, which
    iterates whatever this function finds.
    """
    callers = {(workflow, job) for workflow, job, _ in _jobs_running_the_build_script()}

    assert callers == {
        ("release-artifacts.yml", "homebrew-tap"),
        ("release-publish-pypi.yml", "homebrew-tap-rc"),
        ("macos-brew-smoke.yml", "keg-install"),
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

# `site` imports these two itself, after this hook is already installed --
# they are interpreter startup hooks, not third-party packages, and blocking
# them only makes the interpreter print a ModuleNotFoundError of our own
# making onto the stderr this test reads.
_STARTUP_HOOKS = ("sitecustomize", "usercustomize")


class _ThirdPartyBlocker:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in sys.stdlib_module_names or root in ("nyxgpt", *_STARTUP_HOOKS):
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


# --- The keg venv bootstrap (#3753) ---------------------------------------
#
# Owner acceptance of the rc install path died here: `brew install
# nyxgpt-api@3.0.0rc` on stock Homebrew macOS failed creating the keg venv,
# with `ensurepip --upgrade --default-pip` exiting 1. A plain `python -m venv`
# runs ensurepip implicitly, and ensurepip bootstraps pip from wheels vendored
# in the `python@3.12` keg -- the only step of this install that depends on
# Homebrew-managed keg state rather than on our own tarball.
#
# The recipe now creates the venv with `--without-pip` and puts pip in itself,
# so ensurepip is out of the install path entirely. These tests pin that for
# both formulas that carry the recipe and for what the script actually stamps.

_API_FORMULAS = {
    "local": REPO_ROOT / "homebrew" / "nyxgpt-api.rb",
    "tap-template": REPO_ROOT / "homebrew" / "tap" / "nyxgpt-api.rb.tmpl",
}

_WITHOUT_PIP = 'system python, "-m", "venv", "--without-pip", venv'
_ENSUREPIP_VENV = 'system python, "-m", "venv", venv'
# Round 3 (#3788). The keg's pip is now only allowed to *download* -- see
# `test_the_keg_pip_never_performs_an_install` for why. Pinned as whole lines
# so a drift in the technique fails here, in the Linux suite, rather than on
# the owner's Mac.
_PIP_DOWNLOAD = 'system python, "-m", "pip", "download", "--no-deps", "--only-binary", ":all:",'
_PIP_FROM_WHEEL = 'system venv/"bin/python", "#{pip_wheel}/pip", "install", "--no-index",'
# The spelling that shipped in rc11 and died on python@3.12 3.12.14: it hands
# the keg's own pip an install to perform.
_PIP_INSTALL_VIA_KEG_PIP = (
    'system python, "-m", "pip", "--python", venv/"bin/python", "install", "--upgrade", "pip"'
)


def _venv_recipe(text: str) -> list[str]:
    """The keg's venv bootstrap: code lines from `def install` to the wrapper.

    Comments and blank lines are dropped so this compares what the formula
    *runs*, not how it is documented -- the two files explain themselves with
    different issue references on purpose.
    """
    start = text.index("  def install")
    end = text.index('(bin/"nyxgpt-api").write')
    return [
        line.strip()
        for line in text[start:end].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_keg_venv_is_built_without_ensurepip(which):
    recipe = _venv_recipe(_API_FORMULAS[which].read_text(encoding="utf-8"))

    assert _WITHOUT_PIP in recipe
    # The exact line the owner's install died on.
    assert _ENSUREPIP_VENV not in recipe


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_pip_is_bootstrapped_into_the_keg_venv_from_a_wheel(which):
    """`--without-pip` is only safe because pip is put in deliberately."""
    recipe = _venv_recipe(_API_FORMULAS[which].read_text(encoding="utf-8"))

    assert _PIP_DOWNLOAD in recipe
    assert _PIP_FROM_WHEEL in recipe
    # The wheel has to be found, and a missing one has to stop the install
    # rather than silently leave the venv without a pip.
    assert 'pip_wheel = Dir.glob(wheelhouse/"pip-*.whl").first' in recipe
    assert any(line.startswith("odie ") and "pip_wheel.nil?" in line for line in recipe), recipe
    # Everything downstream still installs through the venv's own pip.
    assert 'system venv/"bin/pip", "install", buildpath' in recipe


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_the_keg_pip_never_performs_an_install(which):
    """#3788: the keg's pip may resolve and download, never install.

    On python@3.12 3.12.14 the Homebrew keg's pip cannot import
    `pip._internal.operations.install.wheel`. pip 26.2 pre-imports its lazy
    imports before writing anything, swallows that ImportError into
    `_MISSING_MODULES`, and its audit hook then turns the real import in
    `req_install.py` into

        ImportError: No module named 'pip._internal.operations.install.wheel'

    So *any* install routed through that pip dies, whatever is being
    installed and in whatever order -- which is why the fix is not a
    reordering but a rule: the only keg-pip subcommand this recipe may use is
    `download`.
    """
    recipe = _venv_recipe(_API_FORMULAS[which].read_text(encoding="utf-8"))

    keg_pip = [line for line in recipe if 'system python, "-m", "pip"' in line]
    assert len(keg_pip) == 1, recipe
    assert '"download"' in keg_pip[0]
    assert '"install"' not in keg_pip[0]
    # The exact rc11 line the owner's install died on.
    assert _PIP_INSTALL_VIA_KEG_PIP not in recipe
    # `pip --python` re-execs the keg's pip in another interpreter; it is the
    # keg's pip either way, so the whole option is out of the recipe.
    assert not any('"--python"' in line for line in recipe), recipe


def test_both_api_formulas_carry_the_same_venv_recipe():
    """The local formula and the tap template are one recipe in two files.

    #3753 had to be fixed in both. Nothing enforced that they agree, so this
    pins it: a bootstrap fix applied to one file and not the other fails here
    rather than on the next owner install.
    """
    local, template = (
        _venv_recipe(path.read_text(encoding="utf-8"))
        for path in (_API_FORMULAS["local"], _API_FORMULAS["tap-template"])
    )

    assert local == template


@pytest.mark.parametrize("channel", ["stable", "rc"])
def test_stamped_formulas_ship_the_ensurepip_free_recipe(
    built_artifacts, built_rc_artifacts, channel
):
    """What is published, not just what is in the templates."""
    out_dir, filename = {
        "stable": (built_artifacts, "nyxgpt-api.rb"),
        "rc": (built_rc_artifacts, "nyxgpt-api@9.9.9rc.rb"),
    }[channel]
    recipe = _venv_recipe((out_dir / filename).read_text(encoding="utf-8"))

    assert _WITHOUT_PIP in recipe
    assert _ENSUREPIP_VENV not in recipe
    # The published formula has to carry the working bootstrap too (#3788).
    assert _PIP_DOWNLOAD in recipe
    assert _PIP_FROM_WHEEL in recipe
    assert _PIP_INSTALL_VIA_KEG_PIP not in recipe


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_rc_conflicts_with_the_name_the_stable_channel_actually_publishes(built_rc_artifacts, name):
    """The conflict target is derived from the stable channel, never typed.

    A `conflicts_with` naming a formula nothing publishes would warn on every
    install (#3753's secondary finding) -- so the name it declares has to be
    the one this same script stamps for the stable channel.
    """
    stable = build_homebrew_artifacts.formula_name(name, "stable")
    formula = (built_rc_artifacts / f"{name}@9.9.9rc.rb").read_text(encoding="utf-8")

    assert f'conflicts_with "{stable}",' in formula


def test_an_absent_stable_counterpart_is_documented_as_benign(built_rc_artifacts):
    """Assessed, not silently left alone: warn-only, kept unconditional."""
    formula = (built_rc_artifacts / "nyxgpt-api@9.9.9rc.rb").read_text(encoding="utf-8")

    assert "install proceeds" in formula
    assert "unknown formula" in formula


# --- The macOS brew smoke job (#3753) -------------------------------------
#
# The brew path was documentation-verified only until this workflow: every
# formula change shipped unexecuted, which is how an install-breaking recipe
# reached the owner's machine. macos-15 runners ship Homebrew, so the install
# is testable for real.


def _macos_smoke_workflow() -> dict:
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / "macos-brew-smoke.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job_run_script(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


@pytest.mark.parametrize("job_name", ["keg-install", "published-tap"])
def test_brew_smoke_runs_on_a_real_macos_runner(job_name):
    job = _macos_smoke_workflow()["jobs"][job_name]

    assert str(job["runs-on"]).startswith("macos")


def test_brew_smoke_installs_the_working_trees_own_recipe():
    """Catches a broken recipe on the PR that writes it, before any publish."""
    script = _job_run_script(_macos_smoke_workflow()["jobs"]["keg-install"])

    assert "scripts/build_homebrew_artifacts.py" in script
    assert "brew install" in script


def test_brew_smoke_verifies_the_pip_ensurepip_used_to_provide():
    """A green `brew install` is not enough -- the venv has to have a pip."""
    script = _job_run_script(_macos_smoke_workflow()["jobs"]["keg-install"])

    assert '"$VENV/bin/pip" --version' in script
    assert "import nyxgpt.app" in script
    assert '"$VENV/bin/nyxgpt" --version' in script


def test_brew_smoke_installs_the_published_candidate_the_way_the_owner_does():
    script = _job_run_script(_macos_smoke_workflow()["jobs"]["published-tap"])

    assert "brew tap" in script
    assert "brew install" in script


def test_brew_smoke_pr_trigger_covers_what_can_break_the_recipe():
    """macOS runners are expensive: paths-filtered, but not past the point."""
    paths = _macos_smoke_workflow()[True]["pull_request"]["paths"]

    assert "homebrew/**" in paths
    assert "scripts/build_homebrew_artifacts.py" in paths


def test_every_rc_cut_smoke_installs_the_candidate_it_published():
    """The owner's failing path, run on the candidate before the owner does."""
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / "release-publish-pypi.yml"
    job = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["macos-brew-smoke"]

    assert job["uses"] == "./.github/workflows/macos-brew-smoke.yml"
    # After the tap push: it installs what was published, so it cannot gate it.
    assert "homebrew-tap-rc" in job["needs"]
    assert job["with"]["formula_version"] == "${{ needs.publish.outputs.version }}"


def test_an_rc_cut_smokes_the_published_tap_and_not_the_checkout():
    """`github.event_name` cannot express this, so an input has to.

    Inside a called workflow `github.event_name` reports the *caller's* event
    (`workflow_dispatch`), never `workflow_call` -- gating the checkout-build
    job on it would silently run a second macOS runner on every rc cut, for a
    recipe the tap job already published.
    """
    workflow = _macos_smoke_workflow()
    condition = str(workflow["jobs"]["keg-install"]["if"])

    assert "github.event_name != 'workflow_call'" not in condition
    assert "inputs.run_keg_install" in condition
    # The caller omits it, so the workflow_call default is what skips the job.
    assert workflow[True]["workflow_call"]["inputs"]["run_keg_install"]["default"] is False


# --- The build-environment mac_ver shim (#3753, round 2) -------------------
#
# With ensurepip gone the owner's install died one step later, inside pip's
# own startup: `platform.mac_ver()` answers with no release in that build
# environment, and pip parses it unguarded both in truststore (its default TLS
# backend since 24.2) and in `packaging.tags.mac_platforms`, so
#
#   ValueError: invalid literal for int() with base 10: ''
#
# takes `brew install` down before a single package is resolved.
#
# The recipe now writes a `sitecustomize.py` into the build environment that
# repairs the value. It is Python nested in a Ruby heredoc, which no linter,
# formatter or import in this repo would otherwise look at -- so these tests
# pull the real source back out of the formulas and *run* it, rather than
# grepping for a spelling of it.


def _build_shim(which: str) -> str:
    return build_homebrew_artifacts.extract_build_shim(
        _API_FORMULAS[which].read_text(encoding="utf-8")
    )


def _shim_namespace(which: str = "local") -> dict:
    """Exec the shipped shim, with its startup block deliberately not run.

    The shim guards its module-level work behind `__name__ == "sitecustomize"`
    precisely so it can be exercised here without patching the interpreter
    running the test suite.
    """
    namespace: dict = {"__file__": "/nonexistent/shim/sitecustomize.py", "__name__": "under_test"}
    exec(compile(_build_shim(which), "<build shim>", "exec"), namespace)
    return namespace


def _truststore_line_22(version: str) -> tuple:
    """The expression the owner's install died on, verbatim."""
    return tuple(map(int, version.split(".")))


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_the_embedded_build_shim_is_valid_python(which):
    """Nothing else in the repo compiles it: a typo would ship to a Mac."""
    compile(_build_shim(which), "<build shim>", "exec")


def test_both_api_formulas_embed_the_same_build_shim():
    """One shim in two files, like the recipe around it (#3753 had to be
    fixed twice for exactly this reason)."""
    assert _build_shim("local") == _build_shim("tap-template")


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_the_build_shim_repairs_an_empty_mac_ver(which, monkeypatch):
    """The whole point: pip's parse of mac_ver() has to stop raising."""
    import platform

    monkeypatch.setattr(platform, "mac_ver", lambda *a, **k: ("", ("", "", ""), ""))
    with pytest.raises(ValueError):
        _truststore_line_22(platform.mac_ver()[0])

    _shim_namespace(which)["_repair_mac_ver"]()

    repaired = platform.mac_ver()[0]
    # No exception, and a version truststore accepts: it refuses < 10.8, and
    # only loads Security.framework by absolute path from 10.16 up.
    assert _truststore_line_22(repaired) >= (10, 16)


def test_the_build_shim_leaves_a_healthy_mac_ver_alone(monkeypatch):
    """It must not invent a version on a Mac whose lookup works.

    mac_ver() also drives `packaging.tags.mac_platforms`, so reporting the
    wrong release would quietly change which wheels pip considers installable.
    """
    import platform

    healthy = ("15.5", ("", "", ""), "arm64")
    monkeypatch.setattr(platform, "mac_ver", lambda *a, **k: healthy)

    _shim_namespace()["_repair_mac_ver"]()

    assert platform.mac_ver() == healthy


def test_the_build_shim_prefers_the_real_release_over_its_floor(monkeypatch):
    """`sw_vers` is asked first -- the constant is only a last resort."""
    import platform
    import subprocess as subprocess_module

    monkeypatch.setattr(platform, "mac_ver", lambda *a, **k: ("", ("", "", ""), ""))

    def fake_run(cmd, **kwargs):
        assert cmd == ["/usr/bin/sw_vers", "-productVersion"]
        return subprocess_module.CompletedProcess(cmd, 0, stdout="15.5\n", stderr="")

    monkeypatch.setattr(subprocess_module, "run", fake_run)

    _shim_namespace()["_repair_mac_ver"]()

    assert platform.mac_ver()[0] == "15.5"


def test_the_build_shim_falls_back_when_sw_vers_is_unavailable_too(monkeypatch):
    """A build environment that hides the OS version may hide `sw_vers` too."""
    import platform
    import subprocess as subprocess_module

    monkeypatch.setattr(platform, "mac_ver", lambda *a, **k: ("", ("", "", ""), ""))

    def exploding_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess_module, "run", exploding_run)

    _shim_namespace()["_repair_mac_ver"]()

    assert _truststore_line_22(platform.mac_ver()[0]) >= (10, 16)


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_the_shim_is_on_pythonpath_before_any_pip_runs(which):
    """Written and exported ahead of the bootstrap, or it fixes nothing."""
    recipe = _venv_recipe(_API_FORMULAS[which].read_text(encoding="utf-8"))
    export = recipe.index('ENV.prepend_path "PYTHONPATH", shim')

    assert recipe.index("(shim/\"sitecustomize.py\").write <<~'PY'") < export
    # The first interpreter that must see it is the one `pip download` runs
    # in -- resolving wheel tags is where mac_ver() is parsed (#3753).
    assert export < recipe.index(_PIP_DOWNLOAD)


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_the_shim_is_a_single_quoted_heredoc(which):
    """`<<~PY` would interpolate `#{}` and expand `\\n` inside the Python."""
    text = _API_FORMULAS[which].read_text(encoding="utf-8")

    assert "<<~'PY'" in text
    assert "<<~PY" not in text


@pytest.mark.parametrize("channel", ["stable", "rc"])
def test_stamped_formulas_ship_the_build_shim(built_artifacts, built_rc_artifacts, channel):
    """What is published, not just what is in the templates."""
    out_dir, filename = {
        "stable": (built_artifacts, "nyxgpt-api.rb"),
        "rc": (built_rc_artifacts, "nyxgpt-api@9.9.9rc.rb"),
    }[channel]
    stamped = (out_dir / filename).read_text(encoding="utf-8")

    assert build_homebrew_artifacts.has_build_shim(stamped)
    assert build_homebrew_artifacts.extract_build_shim(stamped) == _build_shim("tap-template")


def test_publishing_refuses_a_formula_whose_shim_is_broken():
    """The release build is the last place a heredoc typo can still be cheap."""
    broken = (
        _API_FORMULAS["local"]
        .read_text(encoding="utf-8")
        .replace("def _repair_mac_ver():", "def _repair_mac_ver(:")
    )

    with pytest.raises(ValueError, match="not valid Python"):
        build_homebrew_artifacts.validate_build_shim(broken, "nyxgpt-api")


def test_a_formula_with_no_shim_is_not_forced_to_have_one():
    """nyxgpt-web builds with npm and never starts a Python interpreter."""
    web = (REPO_ROOT / "homebrew" / "nyxgpt-web.rb").read_text(encoding="utf-8")

    assert not build_homebrew_artifacts.has_build_shim(web)
    build_homebrew_artifacts.validate_build_shim(web, "nyxgpt-web")


def test_macos_smoke_injects_the_failure_the_runner_does_not_reproduce():
    """rc5's smoke job was green here while the same install failed on a Mac.

    An install-only job is green on any machine that fails to reproduce the
    bug, so the job has to force the condition and prove both directions.
    """
    script = _job_run_script(_macos_smoke_workflow()["jobs"]["keg-install"])

    # The fault, injected rather than hoped for.
    assert 'platform.mac_ver = lambda *args, **kwargs: ("", ("", "", ""), "")' in script
    # The negative control: if pip starts anyway, the run proved nothing.
    assert "invalid literal for int() with base 10: ''" in script
    assert "no longer reproduces #3753" in script
    # And the fix has to visibly fire, not just happen to pass.
    assert "the shim never fired" in script


def test_macos_smoke_injects_the_keg_pip_failure_from_3788():
    """rc11 was the third green-by-luck smoke of the version-drift class.

    The runner had python@3.12 3.12.13_4 and the owner had 3.12.14, so the
    job proved nothing about the pip the owner actually ran. Pulling the keg
    to current narrows the gap but "current" drifts again tomorrow, so the
    failure itself has to be injected and both directions proved.
    """
    script = _job_run_script(_macos_smoke_workflow()["jobs"]["keg-install"])

    # Stop testing whatever pip the runner image was baked with -- and refresh
    # the metadata first, or the upgrade can only reach versions the runner
    # image already knew about and quietly resolves to a no-op. Anchored on
    # the command line proper: the words also appear in the comment and the
    # warning text, so a substring check would survive deleting the command.
    assert any(
        line == "brew update" or line.startswith("brew update ")
        for line in (raw.strip() for raw in script.splitlines())
    )
    assert "brew upgrade python@3.12" in script
    # The condition, injected rather than hoped for: the keg's pip cannot
    # import its own wheel installer, because the module is not there. It is
    # taken away at its realpath -- see the test below for why the path it is
    # imported by is the wrong one to move.
    assert (
        'WHEEL_PY="$("$PY" -c \'import os, '
        "pip._internal.operations.install.wheel as m; "
        "print(os.path.realpath(m.__file__))')\"" in script
    )
    assert 'mv "$WHEEL_PY" "$HIDDEN"' in script
    # The negative control: rc11's bootstrap has to die on it, with the
    # owner's error, or a green result below proves nothing.
    assert "No module named 'pip._internal.operations.install.wheel'" in script
    assert "no longer reproduces #3788" in script
    assert "install --upgrade pip \\" in script
    # And this checkout's bootstrap has to survive the same fault.
    assert '"$WORK/venv-new/bin/pip" --version' in script


def test_the_3788_condition_is_injected_into_the_keg_and_restored():
    """The condition is created, not emulated -- and put back afterwards.

    Two earlier spellings emulated it with a meta-path finder in a
    `sitecustomize` on PYTHONPATH, and both failed on the vehicle rather
    than on the recipe: the first scoped itself with `os.path.abspath` and
    never matched in the symlink-resolved `pip --python` child; the second
    shadowed the keg's own `sitecustomize`, which moved which pip was under
    test. Both times the only symptom was a negative control that passed --
    in a log, indistinguishable from "the bug is gone".

    The general fault is that an emulated condition runs in an interpreter
    environment `brew install` does not have, so the emulation becomes the
    thing under test. Taking the module away is the machine state itself --
    provided it is taken away at its *realpath*. Homebrew links the prefix
    tree into the Cellar, and `pip --python` re-execs through
    `get_runnable_pip()`, which resolves symlinks, so moving the prefix entry
    leaves the child's copy in place: the third red round moved the symlink,
    fired the self-check through the dangling link, and still watched the
    negative control install successfully.

    Three rules hold this in place: the module is moved at its realpath, the
    keg is restored on every exit path (the steps that follow install the
    formulas with that same pip), and the condition is asserted to exist --
    by both routes pip can be imported here -- before anything is inferred
    from it.
    """
    steps = _macos_smoke_workflow()["jobs"]["keg-install"]["steps"]
    script = next(
        str(step["run"])
        for step in steps
        if str(step.get("name", "")).startswith("Reproduce the #3788")
    )

    # No PYTHONPATH emulation: the module is moved aside.
    assert 'mv "$WHEEL_PY" "$HIDDEN"' in script
    assert "sitecustomize" not in script
    assert "PYTHONPATH" not in script
    # ...at the path every route ends at, not the one this process imports by.
    assert "print(os.path.realpath(m.__file__))" in script
    # ...and restored, on the failure paths as well as the success one.
    assert "trap restore_the_keg EXIT" in script
    assert 'print("restored:", m.__file__)' in script
    # The self-check that caught both earlier no-fires, kept and pointed at
    # the machine: its failure has to be loud rather than inferred. It probes
    # the resolved route as well as the prefix one, because checking only the
    # prefix is exactly what let a moved symlink read as a created condition.
    assert "#3788's condition was not created" in script
    assert '("prefix", os.path.dirname(as_imported))' in script
    assert '("resolved (Cellar)", os.path.dirname(resolved))' in script
    # Resolved from the file, like `get_runnable_pip()`. Resolving the
    # directory collapses both routes into one wherever Homebrew links files
    # into a real prefix directory -- which is the common shape.
    assert "resolved = os.path.dirname(os.path.realpath(pip.__file__))" in script


def test_macos_smoke_reads_the_shim_out_of_the_formula_it_ships():
    """A copied shim in the workflow could pass while the formula's is broken."""
    script = _job_run_script(_macos_smoke_workflow()["jobs"]["keg-install"])

    assert "from build_homebrew_artifacts import extract_build_shim" in script
    assert "homebrew/nyxgpt-api.rb" in script


def test_published_tap_reports_whether_the_stable_counterpart_exists():
    """#3753's secondary finding stopped being answerable by guesswork.

    The 2.1.0 backfill was dispatched and the `conflicts_with` warning still
    appeared, so the job now states what the tap actually carries.
    """
    script = _job_run_script(_macos_smoke_workflow()["jobs"]["published-tap"])

    assert "brew info --formula" in script
    assert "conflicts_with" in script


# --- #3850: the keg has to expose the CLI, not just the service wrapper -----
#
# `brew install nyxgpt-api@3.0.0rc` succeeded on the owner's Mac, the
# dashboard came up, and `nyxgpt up` answered `zsh: command not found`.
# pyproject.toml declares the `nyxgpt` console script, so pip did create it --
# inside the keg's venv, which is on nobody's PATH. The formula wrote only
# `bin/nyxgpt-api`, so the entire documented operating surface (`nyxgpt up`,
# `nyxgpt ops ...`) was unreachable on the artifact install path, in breach of
# the operational-command-wrapping rule: with no `nyxgpt` on PATH the operator
# is left with raw commands or nothing.
#
# The formula's own `test do` block asserted the venv python existed and that
# `nyxgpt.app` imported -- both true of a keg with no reachable CLI at all.
# That is why these tests pin the *exposure* and the *test block* together: a
# check that passes against the broken keg is what let this ship.

_CLI_SYMLINK = 'bin.install_symlink venv/"bin/nyxgpt"'
_WEB_FORMULAS = {
    "local": REPO_ROOT / "homebrew" / "nyxgpt-web.rb",
    "tap-template": REPO_ROOT / "homebrew" / "tap" / "nyxgpt-web.rb.tmpl",
}


def _test_block(text: str) -> list[str]:
    """The formula's `test do` body: code lines only, comments dropped."""
    start = text.index("  test do")
    return [
        line.strip()
        for line in text[start:].splitlines()[1:]
        if line.strip() and not line.strip().startswith("#") and line.strip() not in ("end",)
    ]


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_the_keg_puts_the_cli_on_path(which):
    """The fix itself: `bin/` gets the console script, not just the wrapper."""
    recipe = _venv_recipe(_API_FORMULAS[which].read_text(encoding="utf-8"))

    assert _CLI_SYMLINK in recipe
    # Linked from the venv pip populated, so it cannot drift from what was
    # installed -- and only after that install has run.
    assert recipe.index('system venv/"bin/pip", "install", buildpath') < recipe.index(_CLI_SYMLINK)


@pytest.mark.parametrize("which", sorted(_API_FORMULAS))
def test_the_formula_test_block_runs_the_cli(which):
    """`test do` has to fail on a keg whose CLI is unreachable.

    The pre-#3850 block asserted only that the venv python existed and that
    `import nyxgpt.app` worked. Both hold for a keg that exposes no command,
    so the block verified the library rather than the product and stayed
    green through every rc the owner could not operate.
    """
    block = _test_block(_API_FORMULAS[which].read_text(encoding="utf-8"))

    # Reached through `bin`, the path `brew link` exposes -- an assertion
    # against `libexec/venv/bin/nyxgpt` would pass on the broken keg.
    assert any('bin/"nyxgpt"' in line for line in block), block
    assert any('shell_output("#{bin}/nyxgpt --version")' in line for line in block), block
    assert not any("libexec" in line and 'nyxgpt"' in line for line in block), block


def test_both_api_formulas_test_the_keg_the_same_way():
    """One test block in two files, like the recipe above it.

    #3753 had to be fixed in both formulas; an operability assertion added to
    one and not the other would leave the published tap -- the path the owner
    installs from -- asserting less than the local one.
    """
    local, template = (
        _test_block(path.read_text(encoding="utf-8"))
        for path in (_API_FORMULAS["local"], _API_FORMULAS["tap-template"])
    )

    assert local == template


@pytest.mark.parametrize("channel", ["stable", "rc"])
def test_stamped_formulas_expose_and_exercise_the_cli(built_artifacts, built_rc_artifacts, channel):
    """What is published, not just what is in the templates."""
    out_dir, filename = {
        "stable": (built_artifacts, "nyxgpt-api.rb"),
        "rc": (built_rc_artifacts, "nyxgpt-api@9.9.9rc.rb"),
    }[channel]
    stamped = (out_dir / filename).read_text(encoding="utf-8")

    assert _CLI_SYMLINK in _venv_recipe(stamped)
    assert any('bin/"nyxgpt"' in line for line in _test_block(stamped))


@pytest.mark.parametrize("which", sorted(_WEB_FORMULAS))
def test_the_web_keg_exposes_only_its_service_wrapper(which):
    """The #3850 sweep of the other formula, pinned rather than remembered.

    `nyxgpt-web` ships a Next.js build and one command, `nyxgpt-web`, which
    `brew services` runs. It declares no console scripts and nothing
    documented invokes a `nyxgpt-web`-owned binary by any other name -- the
    CLI comes from `nyxgpt-api`. So the class of gap does not repeat here, and
    this test fails if the web formula ever grows a second command whose
    reachability nobody checked.
    """
    text = _WEB_FORMULAS[which].read_text(encoding="utf-8")
    install = text[text.index("  def install") : text.index("  service do")]

    exposed = set(re.findall(r'bin/"([^"]+)"', install))
    exposed |= set(re.findall(r'bin\.install_symlink \S+/"([^"]+)"', install))

    assert exposed == {"nyxgpt-web"}, exposed


def test_brew_smoke_proves_the_cli_is_reachable_by_name():
    """The check that would have caught #3850, and its negative control.

    Reaching into `$VENV/bin/nyxgpt` is what the job did before, and it passed
    on every keg the owner could not operate -- so the assertion has to go
    through PATH, and it has to be shown to fail when the CLI is taken away.
    Unusually for this workflow the condition needs no injection to
    reproduce; only the *check* needs proving.
    """
    steps = _macos_smoke_workflow()["jobs"]["keg-install"]["steps"]
    script = next(
        str(step["run"]) for step in steps if "runnable `nyxgpt`" in str(step.get("name", ""))
    )

    # PATH, by name -- not a path into the keg.
    assert "command -v nyxgpt" in script
    assert "nyxgpt --version" in script
    # The negative control: the same function, run against a keg with no CLI.
    assert 'mv "$KEG_CLI" "$HIDDEN"' in script
    assert "it tests nothing" in script
    # ...and the keg restored on every exit path, since later steps use it.
    assert "trap restore_the_cli EXIT" in script
    # `-e` follows the relative link and would silently skip the restore.
    assert '[ -L "$HIDDEN" ]' in script


@pytest.mark.parametrize("job_name", ["keg-install", "published-tap"])
def test_brew_smoke_runs_a_no_op_subcommand_on_a_stackless_machine(job_name):
    """`--version` resolves the entry point; it does not load the app code.

    `ops status` is documented to always return 0, so on a runner with no
    stack, no Docker and no checkout it is a pure operability probe -- the
    place a repo-relative path resolution (#3759's class of fault) surfaces
    on the artifact path.
    """
    script = _job_run_script(_macos_smoke_workflow()["jobs"][job_name])

    assert "nyxgpt ops status" in script
