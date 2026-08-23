"""The running stack's *tier*, not just its number (#3982).

Owner acceptance found the header could not tell a release candidate from a
release: rc13 kegs were installed and the UI read `v3.0.0`. The version
string alone is not an answer to "what am I testing?" -- the channel it
belongs to is, and it has to come from the same place the version does so
every client agrees.
"""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.version import (
    CHANNEL_DEV,
    CHANNEL_RC,
    CHANNEL_STABLE,
    CHANNEL_UNKNOWN,
    UNKNOWN_VERSION,
    version_channel,
)

pytestmark = pytest.mark.unit


class TestVersionChannel:
    """`version_channel()` classifies what an operator is actually running."""

    @pytest.mark.parametrize("value", ["3.0.0", "3.0", "12.4.1", "v3.0.0"])
    def test_final_releases_are_stable(self, value):
        assert version_channel(value) == CHANNEL_STABLE

    @pytest.mark.parametrize(
        "value",
        ["3.0.0rc13", "v3.0.0rc13", "3.0.0rc1", "2.1.0b2", "2.1.0a1", " 3.0.0rc13 "],
    )
    def test_prereleases_are_release_candidates(self, value):
        """The rc suffix is the whole difference between candidate and release."""
        assert version_channel(value) == CHANNEL_RC

    @pytest.mark.parametrize(
        "value",
        ["3.0.0.dev1", "3.0.0+local", "local", ":local", "3.0.0+dirty", "main"],
    )
    def test_working_tree_builds_are_dev(self, value):
        """A `:local` k8s image or a `.devN` build is neither rc nor stable.

        Ordering guard: PEP 440 counts `.devN` as a pre-release, so a naive
        rc-first check would report `3.0.0.dev1` as a published candidate and
        send an operator looking for an rc that was never cut.
        """
        assert version_channel(value) == CHANNEL_DEV

    @pytest.mark.parametrize("value", [None, "", "   ", UNKNOWN_VERSION])
    def test_absent_version_is_unknown_not_a_guess(self, value):
        assert version_channel(value) == CHANNEL_UNKNOWN

    def test_a_candidate_is_never_reported_as_a_release(self):
        """The defect in one line: rc13 must not read the same as 3.0.0."""
        assert version_channel("3.0.0rc13") != version_channel("3.0.0")


class TestInfoEndpointChannel:
    """GET /api/v1/info carries the channel beside the version."""

    def _info(self, running: str):
        with (
            patch("nyxgpt.app.running_version", return_value=running),
            patch("nyxgpt.app.load_config", return_value=ConfigParser()),
        ):
            return TestClient(app).get("/api/v1/info").json()

    def test_release_candidate_reports_the_rc_channel_and_keeps_its_suffix(self):
        data = self._info("3.0.0rc13")

        # End to end from package metadata: the suffix survives into the
        # payload the header renders. Truncating it here is what made rc and
        # stable indistinguishable in the UI.
        assert data["release_version"] == "3.0.0rc13"
        assert data["release_channel"] == CHANNEL_RC

    def test_stable_release_reports_the_stable_channel(self):
        data = self._info("3.0.0")

        assert data["release_version"] == "3.0.0"
        assert data["release_channel"] == CHANNEL_STABLE

    def test_undeterminable_version_reports_unknown_not_stable(self):
        data = self._info(UNKNOWN_VERSION)

        assert data["release_channel"] == CHANNEL_UNKNOWN
