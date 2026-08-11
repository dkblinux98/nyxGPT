"""Unit tests for scripts/build_homebrew_artifacts.py (#3622).

Runs the release script end-to-end against the real repo checkout (it
needs real pyproject.toml/src/nyxgpt/web trees to vendor -- same
precondition `_create_dist_tarball` already has) and asserts the stamped
formulas are well-formed and placeholder-free.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_homebrew_artifacts.py"
BASE_URL = "https://github.com/dkblinux98/nyxGPT/releases/download/9.9.9"


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
    """The rc channel's output: `@rc` formulas, and nothing else (#3727)."""
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

    assert written == ["nyxgpt-api@rc.rb", "nyxgpt-web@rc.rb"]
    assert not (built_rc_artifacts / "nyxgpt-api.rb").exists()
    assert not (built_rc_artifacts / "nyxgpt-web.rb").exists()


@pytest.mark.parametrize(
    ("name", "class_name"),
    [("nyxgpt-api", "NyxgptApiATRc"), ("nyxgpt-web", "NyxgptWebATRc")],
)
def test_rc_formula_declares_homebrews_class_name_for_an_at_formula(
    built_rc_artifacts, name, class_name
):
    """`brew` derives the class from the file name -- a mismatch fails to load."""
    formula = (built_rc_artifacts / f"{name}@rc.rb").read_text(encoding="utf-8")

    assert f"class {class_name} < Formula" in formula


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_rc_formula_conflicts_with_its_stable_counterpart(built_rc_artifacts, name):
    formula = (built_rc_artifacts / f"{name}@rc.rb").read_text(encoding="utf-8")

    assert f'conflicts_with "{name}",' in formula
    assert "because:" in formula


@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_rc_formula_is_stamped_with_the_real_rc_tarball(built_rc_artifacts, name):
    tarball = built_rc_artifacts / "dist" / f"{name}-9.9.9rc4.tar.gz"
    assert tarfile.is_tarfile(tarball)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()

    formula = (built_rc_artifacts / f"{name}@rc.rb").read_text(encoding="utf-8")

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
    rc = (built_rc_artifacts / f"{name}@rc.rb").read_text(encoding="utf-8")

    # Everything from `def install` down is byte-identical -- the keg an RC
    # installs is the same recipe as the release's, on a different tarball.
    assert stable[stable.index("  def install") :] == rc[rc.index("  def install") :]
