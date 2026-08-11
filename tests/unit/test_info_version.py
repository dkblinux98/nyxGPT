"""Unit tests for the running-version sourcing of GET /api/v1/info (#3716).

The version shown to a user must be the version that is actually running --
the installed `nyxgpt` package version -- never the agent tooling's
`[github] RELEASE_BRANCH` config value, which drifts (the badge showed a
stale `v1.0.0` while 3.0.0 was installed).
"""

from __future__ import annotations

import tomllib
from configparser import ConfigParser
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.version import UNKNOWN_VERSION, running_version

pytestmark = pytest.mark.unit


def _cfg_with_release_branch(branch: str) -> ConfigParser:
    """Build a config whose `[github] RELEASE_BRANCH` is deliberately stale."""
    cfg = ConfigParser()
    cfg.add_section("github")
    cfg.set("github", "RELEASE_BRANCH", branch)
    return cfg


class TestRunningVersion:
    """`running_version()` reads installed package metadata."""

    def test_returns_installed_package_version(self):
        with patch("nyxgpt.version.version", return_value="3.0.0") as mock_version:
            assert running_version() == "3.0.0"
        mock_version.assert_called_once_with("nyxgpt")

    def test_falls_back_to_pyproject_without_package_metadata(self, tmp_path):
        """A bare source tree (no dist-info) still reports the checkout's version."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "nyxGPT"\nversion = "9.9.9"\n', encoding="utf-8")
        fake_module = tmp_path / "pkg" / "nyxgpt" / "version.py"

        with (
            patch("nyxgpt.version.version", side_effect=PackageNotFoundError("nyxgpt")),
            patch("nyxgpt.version.__file__", str(fake_module)),
        ):
            assert running_version() == "9.9.9"

    def test_falls_back_to_unknown_when_pyproject_unreadable(self, tmp_path):
        fake_module = tmp_path / "pkg" / "nyxgpt" / "version.py"

        with (
            patch("nyxgpt.version.version", side_effect=PackageNotFoundError("nyxgpt")),
            patch("nyxgpt.version.__file__", str(fake_module)),
        ):
            assert running_version() == UNKNOWN_VERSION

    def test_matches_pyproject_version_in_this_dev_tree(self):
        """`pip install -e .` mode: metadata agrees with the checkout's pyproject."""
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

        assert running_version() == declared


class TestInfoEndpointVersion:
    """GET /api/v1/info reports the running version, not a config value."""

    def test_release_version_is_running_version_not_release_branch(self):
        with (
            patch("nyxgpt.app.running_version", return_value="3.0.0"),
            patch("nyxgpt.app.load_config", return_value=_cfg_with_release_branch("v1.0.0")),
        ):
            response = TestClient(app).get("/api/v1/info")

        assert response.status_code == 200
        data = response.json()
        assert data["release_version"] == "3.0.0"
        assert data["release_version"] != "v1.0.0"

    def test_release_branch_exposed_under_its_own_field(self):
        with (
            patch("nyxgpt.app.running_version", return_value="3.0.0"),
            patch("nyxgpt.app.load_config", return_value=_cfg_with_release_branch("v1.0.0")),
        ):
            response = TestClient(app).get("/api/v1/info")

        assert response.json()["release_branch"] == "v1.0.0"

    def test_release_version_reported_without_github_config_section(self):
        """The badge must not depend on `[github] RELEASE_BRANCH` existing at all."""
        with (
            patch("nyxgpt.app.running_version", return_value="3.0.0"),
            patch("nyxgpt.app.load_config", return_value=ConfigParser()),
        ):
            response = TestClient(app).get("/api/v1/info")

        assert response.status_code == 200
        data = response.json()
        assert data["release_version"] == "3.0.0"
        assert data["release_branch"] is None
