"""Unit tests for installing the native services from published artifacts (#3759).

`nyxgpt ops install` used to build the `api`/`web` services by vendoring
source out of `REPO_ROOT`. On an artifact install (`pip install nyxgpt`, the
repo-less portability requirement) that path resolves *inside* the installed
venv -- `.../venv/lib/python3.11/src/nyxgpt` -- so the api step died with
`FileNotFoundError` on a clean EC2 instance and the web/mcp dep steps
reported nonsense paths.

Every test here therefore runs with `REPO_ROOT` pointed at a directory that
is deliberately NOT a checkout, which is what an installed wheel sees.
Nothing real is downloaded, built or started: `httpx.stream`, `_run` and
`subprocess.run` are all mocked, the same way test_ops_systemd.py mocks the
dev-checkout path it mirrors.
"""

import json
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from nyxgpt import ops

pytestmark = pytest.mark.unit


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def artifact_root(monkeypatch, tmp_path):
    """Point `REPO_ROOT` at an installed venv's site-packages parent (no checkout)."""
    root = tmp_path / "venv" / "lib" / "python3.11"
    root.mkdir(parents=True)
    monkeypatch.setattr(ops, "REPO_ROOT", root)
    return root


def _checkout(tmp_path: Path) -> Path:
    """Build a minimal dev checkout: both services' vendorable source."""
    repo = tmp_path / "repo"
    (repo / "src" / "nyxgpt").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text("{}", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    (repo / "example.config.ini").write_text("[api]\nhost = 127.0.0.1\n", encoding="utf-8")
    return repo


class _FakeStream:
    """Stand-in for `httpx.stream(...)`'s context manager."""

    def __init__(self, chunks=(b"tarball-bytes",), status_error=None):
        self._chunks = chunks
        self._status_error = status_error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def iter_bytes(self):
        yield from self._chunks


# --- source detection --------------------------------------------------


def test_has_vendorable_source_true_for_a_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", _checkout(tmp_path))

    assert ops._has_vendorable_source("nyxgpt-api") is True
    assert ops._has_vendorable_source("nyxgpt-web") is True


def test_has_vendorable_source_false_for_an_artifact_install(artifact_root):
    assert ops._has_vendorable_source("nyxgpt-api") is False
    assert ops._has_vendorable_source("nyxgpt-web") is False


def test_has_vendorable_source_is_per_service(monkeypatch, tmp_path):
    """A checkout missing `web/` can still vendor the api tarball, and vice versa."""
    repo = _checkout(tmp_path)
    (repo / "web" / "package.json").unlink()
    monkeypatch.setattr(ops, "REPO_ROOT", repo)

    assert ops._has_vendorable_source("nyxgpt-api") is True
    assert ops._has_vendorable_source("nyxgpt-web") is False


# --- version resolution ------------------------------------------------


def test_native_service_version_reads_the_checkout_pyproject(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", _checkout(tmp_path))
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "1.2.3")

    assert ops._native_service_version() == "9.9.9"


def test_native_service_version_falls_back_to_installed_metadata(artifact_root, monkeypatch):
    """No pyproject above the package: the installed distribution answers."""
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0rc5")

    assert ops._native_service_version() == "3.0.0rc5"


def test_native_service_version_without_metadata_or_pyproject(artifact_root, monkeypatch):
    def _missing(_name):
        raise ops.importlib.metadata.PackageNotFoundError("nyxgpt")

    monkeypatch.setattr(ops.importlib.metadata, "version", _missing)

    assert ops._native_service_version() == "0.0.0"


# --- downloading the published tarball ---------------------------------


def test_download_release_tarball_writes_the_asset(monkeypatch, tmp_path):
    seen = {}

    def _fake_stream(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        return _FakeStream()

    monkeypatch.setattr(ops.httpx, "stream", _fake_stream)

    tar = ops._download_release_tarball(tmp_path, "nyxgpt-api", "3.0.0rc5")

    assert seen["method"] == "GET"
    assert seen["url"] == (
        "https://github.com/dkblinux98/nyxGPT/releases/download/3.0.0rc5/"
        "nyxgpt-api-3.0.0rc5.tar.gz"
    )
    # Same filename and dist/ location `_create_dist_tarball` produces, so
    # the callers can't tell the two sources apart.
    assert tar == tmp_path / "dist" / "nyxgpt-api-3.0.0rc5.tar.gz"
    assert tar.read_bytes() == b"tarball-bytes"


def test_download_release_tarball_falls_back_to_the_homebrew_release(monkeypatch, tmp_path):
    """A stable release cannot carry its own tarballs (#3763).

    Releases in this repository are immutable, so a release published before
    its Homebrew tarballs were built can never gain them -- they are
    published on a `<version>-homebrew` release instead, which is where the
    stamped formulas point too. An artifact install has to look there.
    """
    tried = []

    def _fake_stream(method, url, **kwargs):
        tried.append(url)
        if "-homebrew/" not in url:
            return _FakeStream(status_error=ops.httpx.HTTPError("404 Not Found"))
        return _FakeStream()

    monkeypatch.setattr(ops.httpx, "stream", _fake_stream)

    tar = ops._download_release_tarball(tmp_path, "nyxgpt-api", "2.1.0")

    assert tried == [
        "https://github.com/dkblinux98/nyxGPT/releases/download/2.1.0/nyxgpt-api-2.1.0.tar.gz",
        "https://github.com/dkblinux98/nyxGPT/releases/download/2.1.0-homebrew/"
        "nyxgpt-api-2.1.0.tar.gz",
    ]
    # Same filename either source served it from.
    assert tar == tmp_path / "dist" / "nyxgpt-api-2.1.0.tar.gz"
    assert tar.read_bytes() == b"tarball-bytes"
    # The 404'd first attempt left no partial download behind.
    assert not list((tmp_path / "dist").glob(".nyxgpt-api*"))


def test_download_release_tarball_reports_the_url_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ops.httpx,
        "stream",
        lambda *a, **k: _FakeStream(status_error=ops.httpx.HTTPError("404 Not Found")),
    )

    with pytest.raises(RuntimeError) as excinfo:
        ops._download_release_tarball(tmp_path, "nyxgpt-web", "9.9.9")

    message = str(excinfo.value)
    assert "nyxgpt-web-9.9.9.tar.gz" in message
    assert "404 Not Found" in message
    # Both places it looked, so the diagnosis names what was actually tried.
    assert "download/9.9.9/" in message
    assert "download/9.9.9-homebrew/" in message
    # No truncated archive left behind for the next run to install from.
    assert not list((tmp_path / "dist").glob(".nyxgpt-web*"))
    assert not (tmp_path / "dist" / "nyxgpt-web-9.9.9.tar.gz").exists()


def test_service_source_tarball_vendors_from_a_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", _checkout(tmp_path))
    monkeypatch.setattr(
        ops,
        "_download_release_tarball",
        lambda *a, **k: pytest.fail("a checkout must not download"),
    )
    monkeypatch.setattr(ops, "_create_dist_tarball", lambda *a, **k: Path("/vendored.tar.gz"))

    assert ops._service_source_tarball(tmp_path, "nyxgpt-api", "9.9.9") == Path("/vendored.tar.gz")


def test_service_source_tarball_downloads_on_an_artifact_install(artifact_root, monkeypatch):
    monkeypatch.setattr(
        ops,
        "_create_dist_tarball",
        lambda *a, **k: pytest.fail("an artifact install has no source to vendor"),
    )
    monkeypatch.setattr(ops, "_download_release_tarball", lambda *a, **k: Path("/published.tar.gz"))

    assert ops._service_source_tarball(artifact_root, "nyxgpt-api", "3.0.0rc5") == Path(
        "/published.tar.gz"
    )


# --- Pre-staged assets (NYXGPT_ARTIFACT_DIR) ----------------------------
#
# The third source, for a version no release serves: `nyxgpt cloud smoke
# --container --wheel` (#3784) builds a branch's own service tarballs and
# stages them, and an air-gapped install has the assets but no github.com.


def test_service_source_tarball_prefers_a_staged_asset_over_downloading(
    artifact_root, monkeypatch, tmp_path
):
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    (staged_dir / "nyxgpt-api-3.0.0.tar.gz").write_bytes(b"staged bytes")
    monkeypatch.setenv(ops.LOCAL_ARTIFACT_DIR_ENV, str(staged_dir))
    monkeypatch.setattr(
        ops,
        "_download_release_tarball",
        lambda *a, **k: pytest.fail("a staged asset must not be downloaded over"),
    )

    tar = ops._service_source_tarball(artifact_root, "nyxgpt-api", "3.0.0")

    # Same filename and same `dist/` location a download would have produced,
    # so nothing downstream can tell the two apart.
    assert tar == artifact_root / "dist" / "nyxgpt-api-3.0.0.tar.gz"
    assert tar.read_bytes() == b"staged bytes"


def test_a_staged_dir_missing_the_asset_says_so_instead_of_downloading(
    artifact_root, monkeypatch, tmp_path
):
    """A staging bug must not read as a network failure against a URL nobody chose."""
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    monkeypatch.setenv(ops.LOCAL_ARTIFACT_DIR_ENV, str(staged_dir))
    monkeypatch.setattr(
        ops,
        "_download_release_tarball",
        lambda *a, **k: pytest.fail("an explicit staging directory must not be second-guessed"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        ops._service_source_tarball(artifact_root, "nyxgpt-web", "3.0.0")

    message = str(excinfo.value)
    assert "nyxgpt-web-3.0.0.tar.gz" in message
    assert str(staged_dir) in message


def test_an_unset_staging_dir_leaves_the_published_download_path_alone(artifact_root, monkeypatch):
    monkeypatch.delenv(ops.LOCAL_ARTIFACT_DIR_ENV, raising=False)
    monkeypatch.setattr(ops, "_download_release_tarball", lambda *a, **k: Path("/published.tar.gz"))

    assert ops._service_source_tarball(artifact_root, "nyxgpt-api", "3.0.0rc11") == Path(
        "/published.tar.gz"
    )


def test_a_checkout_ignores_the_staging_dir_and_still_vendors(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", _checkout(tmp_path))
    monkeypatch.setenv(ops.LOCAL_ARTIFACT_DIR_ENV, str(tmp_path / "nonexistent"))
    monkeypatch.setattr(ops, "_create_dist_tarball", lambda *a, **k: Path("/vendored.tar.gz"))

    assert ops._service_source_tarball(tmp_path, "nyxgpt-api", "9.9.9") == Path("/vendored.tar.gz")


# --- Linux native api, artifact install --------------------------------


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")


def test_install_native_api_systemd_from_published_artifact(
    linux, artifact_root, monkeypatch, tmp_path
):
    """The #3759 regression: this used to raise FileNotFoundError on `src/nyxgpt`."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0rc5")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))
    monkeypatch.setattr(
        ops,
        "_create_dist_tarball",
        lambda *a, **k: pytest.fail("must not vendor from a nonexistent checkout"),
    )

    downloaded = {}

    def _fake_download(dest_dir, name, version):
        downloaded["name"] = name
        downloaded["version"] = version
        tar = dest_dir / "dist" / f"{name}-{version}.tar.gz"
        tar.parent.mkdir(parents=True, exist_ok=True)
        tar.write_bytes(b"x")
        return tar

    monkeypatch.setattr(ops, "_download_release_tarball", _fake_download)
    tpl = tmp_path / "nyxgpt-api.service"
    tpl.write_text("ExecStart=__NYXGPT_HOME__/.nyxGPT/opt/nyxgpt-api/bin/nyxgpt-api\n")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))

    results = ops._install_native_api_systemd()

    assert all(r.ok for r in results), results
    assert downloaded == {"name": "nyxgpt-api", "version": "3.0.0rc5"}
    wrapper = home / ".nyxGPT" / "opt" / "nyxgpt-api" / "bin" / "nyxgpt-api"
    assert wrapper.exists()
    assert "uvicorn nyxgpt.app:app" in wrapper.read_text(encoding="utf-8")
    assert (home / ".config" / "systemd" / "user" / "nyxgpt-api.service").exists()


def test_install_native_api_systemd_reports_a_failed_download(
    linux, artifact_root, monkeypatch, tmp_path
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0rc5")
    monkeypatch.setattr(
        ops,
        "_download_release_tarball",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Could not download ...: timed out")),
    )
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: pytest.fail("no venv should be created without an artifact")
    )

    results = ops._install_native_api_systemd()

    assert len(results) == 1
    assert results[0].ok is False
    assert "Could not obtain the nyxgpt-api artifact" in results[0].message
    assert "timed out" in results[0].details


# --- Linux native web, artifact install --------------------------------


def _fake_web_tarball(dest_dir: Path, version: str) -> Path:
    """A published-looking `nyxgpt-web-<version>.tar.gz` the installer can extract."""
    staged = dest_dir / "src" / f"nyxgpt-web-{version}"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "package.json").write_text("{}", encoding="utf-8")
    tar_path = dest_dir / "dist" / f"nyxgpt-web-{version}.tar.gz"
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(staged, arcname=f"nyxgpt-web-{version}")
    return tar_path


def test_install_native_web_systemd_from_published_artifact(
    linux, artifact_root, monkeypatch, tmp_path
):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0rc5")
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))
    monkeypatch.setattr(ops.subprocess, "run", lambda cmd, **k: _cp(0))
    monkeypatch.setattr(
        ops,
        "_create_dist_tarball",
        lambda *a, **k: pytest.fail("must not vendor from a nonexistent checkout"),
    )
    monkeypatch.setattr(
        ops,
        "_download_release_tarball",
        lambda dest_dir, name, version: _fake_web_tarball(tmp_path / "assets", version),
    )
    tpl = tmp_path / "nyxgpt-web.service"
    tpl.write_text("ExecStart=__NYXGPT_HOME__/.nyxGPT/opt/nyxgpt-web/bin/nyxgpt-web\n")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))

    results = ops._install_native_web_systemd()

    assert all(r.ok for r in results), results
    wrapper = home / ".nyxGPT" / "opt" / "nyxgpt-web" / "bin" / "nyxgpt-web"
    assert wrapper.exists()
    assert "npm run start" in wrapper.read_text(encoding="utf-8")
    assert (home / ".config" / "systemd" / "user" / "nyxgpt-web.service").exists()


def test_install_native_web_systemd_reports_a_failed_download(
    linux, artifact_root, monkeypatch, tmp_path
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0rc5")
    monkeypatch.setattr(
        ops,
        "_download_release_tarball",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Could not download ...: 404")),
    )

    results = ops._install_native_web_systemd()

    assert len(results) == 1
    assert results[0].ok is False
    assert "Could not obtain the nyxgpt-web artifact" in results[0].message


# --- macOS, artifact install -------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [
        ("3.0.0", "nyxgpt-api"),
        ("2.1.0", "nyxgpt-api"),
        ("3.0.0rc5", "nyxgpt-api@3.0.0rc"),
        ("3.1.0rc12", "nyxgpt-api@3.1.0rc"),
    ],
)
def test_remote_tap_formula_names_the_channel(version, expected):
    assert ops._remote_tap_formula("nyxgpt-api", version) == expected


def test_install_homebrew_api_uses_the_published_tap_without_a_checkout(artifact_root, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: (calls.append(list(cmd)), _cp(0))[1])
    monkeypatch.setattr(ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, name)])
    monkeypatch.setattr(
        ops,
        "_create_dist_tarball",
        lambda *a, **k: pytest.fail("an artifact install has no source to vendor"),
    )

    results = ops._install_homebrew_api()

    assert all(r.ok for r in results), results
    assert ["brew", "tap", "dkblinux98/nyxgpt"] in calls
    assert ["brew", "tap-trust", "dkblinux98/nyxgpt"] in calls
    assert ["brew", "install", "dkblinux98/nyxgpt/nyxgpt-api"] in calls
    assert results[-1].message == "nyxgpt-api"
    # The tap, never one formula: `conflicts_with` makes brew load the
    # counterpart formula, which a per-formula grant leaves untrusted (#3770).
    assert not any("--formula" in call for call in calls)


def test_the_tap_trust_step_falls_back_to_the_other_spelling(artifact_root, monkeypatch):
    """`brew tap-trust` failing does not mean the tap is trusted (#3770).

    Homebrew spells the whole-tap grant `tap-trust` on some builds and
    `trust` on others. Treating the first one's "unknown command" as the end
    of the story is how the published-tap smoke job ran untrusted for two
    rc cuts, so an install that needs the gate open tries both.
    """
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _cp(1 if list(cmd)[:2] == ["brew", "tap-trust"] else 0)

    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0")
    monkeypatch.setattr(ops, "_run", _fake_run)
    monkeypatch.setattr(ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_homebrew_api()

    assert all(r.ok for r in results), results
    assert ["brew", "trust", "dkblinux98/nyxgpt"] in calls
    # Trust has to land before the install it exists to unblock.
    assert calls.index(["brew", "trust", "dkblinux98/nyxgpt"]) < calls.index(
        ["brew", "install", "dkblinux98/nyxgpt/nyxgpt-api"]
    )


def test_neither_trust_spelling_existing_does_not_fail_the_install(artifact_root, monkeypatch):
    """A Homebrew old enough not to gate third-party taps has nothing to trust."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _cp(1 if "trust" in list(cmd)[1] else 0)

    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0")
    monkeypatch.setattr(ops, "_run", _fake_run)
    monkeypatch.setattr(ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_homebrew_api()

    assert all(r.ok for r in results), results
    assert ["brew", "install", "dkblinux98/nyxgpt/nyxgpt-api"] in calls


def test_install_homebrew_web_uses_the_published_tap_without_a_checkout(artifact_root, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0rc5")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: (calls.append(list(cmd)), _cp(0))[1])
    monkeypatch.setattr(ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_homebrew_web()

    assert all(r.ok for r in results), results
    assert ["brew", "install", "dkblinux98/nyxgpt/nyxgpt-web@3.0.0rc"] in calls
    # A candidate keg's service is named after its formula, which the
    # stable-named status/self-heal lookups don't track -- say so.
    assert "docs/homebrew.md#candidate-channel" in results[0].details


def _fake_remote_keg(tmp_path, formula="nyxgpt-api", version="3.0.0"):
    """A Cellar/opt layout `_verify_installed_brew_keg` will accept (#3861)."""
    keg = tmp_path / "Cellar" / formula / version
    (keg / "bin").mkdir(parents=True)
    exe = keg / "bin" / formula
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    (keg / "INSTALL_RECEIPT.json").write_text(
        json.dumps({"time": int(time.time())}), encoding="utf-8"
    )
    (tmp_path / "opt").mkdir(parents=True, exist_ok=True)
    (tmp_path / "opt" / formula).symlink_to(keg)
    (tmp_path / "bin").mkdir(exist_ok=True)
    return keg


def _brew_dirs(cmd, tmp_path):
    """Answer `brew --cellar`/`--prefix`/`linkage` out of `tmp_path`, else None."""
    if cmd[:2] == ["brew", "--cellar"]:
        return _cp(0, stdout=f"{tmp_path / 'Cellar'}\n")
    if cmd[:2] == ["brew", "--prefix"]:
        return _cp(0, stdout=f"{tmp_path}\n")
    if cmd[:2] == ["brew", "linkage"]:
        return _cp(0)
    return None


def test_install_from_remote_tap_falls_back_to_upgrade(artifact_root, monkeypatch, tmp_path):
    """An already-installed formula the tap has moved past is an upgrade.

    The fallback survives #3861, but its verdict no longer comes from the
    upgrade's exit code -- see the companion test below for why that mattered.
    """
    calls = []
    _fake_remote_keg(tmp_path)

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        dirs = _brew_dirs(cmd, tmp_path)
        if dirs is not None:
            return dirs
        if cmd[:2] == ["brew", "install"]:
            return _cp(1, stderr="already installed")
        return _cp(0)

    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0")
    monkeypatch.setattr(ops, "_run", _run)
    monkeypatch.setattr(ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_from_remote_tap("nyxgpt-api")

    assert all(r.ok for r in results), results
    assert ["brew", "upgrade", "dkblinux98/nyxgpt/nyxgpt-api"] in calls


def test_install_from_remote_tap_does_not_trust_a_no_op_upgrade(
    artifact_root, monkeypatch, tmp_path
):
    """The divergence named in #3861's acceptance comment, closed.

    `brew upgrade` exits 0 when it does nothing at all -- "already up-to-date"
    is a success as far as the status is concerned. So this path reported `ok`
    for exactly the class of failure `_brew_install_or_reinstall` was calling
    fatal, and the two install routes disagreed about the same machine state
    by accident rather than by design. Both now decide on the keg.
    """
    (tmp_path / "Cellar").mkdir(parents=True)
    (tmp_path / "bin").mkdir(parents=True)

    def _run(cmd, **kwargs):
        dirs = _brew_dirs(cmd, tmp_path)
        if dirs is not None:
            return dirs
        if cmd[:2] == ["brew", "install"]:
            return _cp(1, stderr="Error: Failure while executing; `pip` exited 1")
        return _cp(0)  # including `brew upgrade`, which "succeeds" doing nothing

    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0")
    monkeypatch.setattr(ops, "_run", _run)
    monkeypatch.setattr(ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_from_remote_tap("nyxgpt-api")

    assert results[0].ok is False
    assert "no installed keg for nyxgpt-api" in results[0].details


def test_install_from_remote_tap_survives_a_post_install_soft_failure(
    artifact_root, monkeypatch, tmp_path
):
    """And the same tolerance as the local-tap path, for the same reason (#3861)."""
    _fake_remote_keg(tmp_path)

    def _run(cmd, **kwargs):
        dirs = _brew_dirs(cmd, tmp_path)
        if dirs is not None:
            return dirs
        if cmd[:2] == ["brew", "install"]:
            return _cp(1, stderr="Error: Failed to fix install linkage")
        if cmd[:2] == ["brew", "upgrade"]:
            raise AssertionError("a verified soft failure must not fall through to upgrade")
        return _cp(0)

    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0")
    monkeypatch.setattr(ops, "_run", _run)
    monkeypatch.setattr(ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_from_remote_tap("nyxgpt-api")

    assert all(r.ok for r in results), results
    assert "Failed to fix install linkage" in results[0].details
    assert results[0].status == "WARN"


def test_install_from_remote_tap_reports_both_failures(artifact_root, monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda _n: "3.0.0")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: _cp(0) if cmd[:2] == ["brew", "tap"] else _cp(1, stderr="no such formula"),
    )

    results = ops._install_from_remote_tap("nyxgpt-api")

    assert len(results) == 1
    assert results[0].ok is False
    assert "Failed to install dkblinux98/nyxgpt/nyxgpt-api" in results[0].message
    assert "no such formula" in results[0].details


def test_install_from_remote_tap_needs_brew(artifact_root, monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda name: None)

    results = ops._install_from_remote_tap("nyxgpt-api")

    assert results == [ops.OpsResult(False, "Homebrew not found", "")]


# --- the dep steps that reported checkout paths ------------------------


def test_ensure_web_deps_is_not_applicable_to_an_artifact_install(artifact_root, monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda name: pytest.fail("node/npm are irrelevant without a checkout")
    )

    results = ops._ensure_web_deps()

    assert len(results) == 1
    assert results[0].ok is True
    assert "not applicable to an artifact install" in results[0].message
    # The old message pasted the venv-internal path REPO_ROOT resolved to.
    assert str(artifact_root) not in results[0].details


def test_ensure_mcp_deps_is_not_applicable_to_an_artifact_install(artifact_root, monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda name: pytest.fail("npm is irrelevant without a checkout")
    )

    results = ops._ensure_mcp_deps()

    assert len(results) == 1
    assert results[0].ok is True
    assert "not applicable to an artifact install" in results[0].message
    assert str(artifact_root) not in results[0].details
