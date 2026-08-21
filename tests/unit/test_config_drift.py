"""`find_config_drift` must see the sections the wizard's stale-key check cannot (#3976).

`find_stale_keys` iterates `WIZARD_SCHEMA`, which is `example.config.ini`
minus `EXCLUDED_SECTIONS` -- so it is structurally blind in `[github]`,
`[paths]`, `[homebrew]`, `[pypi]`, `[openai]` and `[cloud]`, which is where
the credentials are. All eight keys found drifting on 2026-08-20 were in those
sections, and the detector could not have reported a single one. The first
test here is that difference stated as an assertion; the rest pin the
behaviour `nyxgpt ops config-drift` reports.
"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest

from nyxgpt import config, config_wizard

pytestmark = pytest.mark.unit

_EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "example.config.ini"


def _example_as_a_users_config() -> ConfigParser:
    """A config.ini that carries exactly what `example.config.ini` declares.

    Parsed with the default `optionxform` (lowercasing), the way
    `config.load_config` reads a real file -- which is also what makes the
    case-insensitive comparison in `find_config_drift` load-bearing.
    """
    parser = ConfigParser()
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")
    return parser


def test_a_config_matching_the_example_reports_no_drift() -> None:
    """The baseline. Without it, every other assertion here could be noise."""
    assert config_wizard.find_config_drift(_example_as_a_users_config()) == {
        "undeclared": [],
        "missing": [],
    }


def test_an_undeclared_key_in_an_excluded_section_is_reported() -> None:
    """The exact blind spot: `[github]` is excluded, so `find_stale_keys` is silent.

    Fault injection for the fix -- the same config is handed to both
    detectors, and only the new one names the key.
    """
    cfg = _example_as_a_users_config()
    cfg.set("github", "some_hand_added_token", "value-that-must-not-be-printed")

    assert config_wizard.find_config_drift(cfg)["undeclared"] == ["github.some_hand_added_token"]
    assert "github" not in config_wizard.find_stale_keys(cfg)


def test_an_undeclared_key_in_a_wizard_section_is_reported_too() -> None:
    """The new check is a superset of the old one, not a replacement for it."""
    cfg = _example_as_a_users_config()
    cfg.set("monitoring", "retired_option", "x")

    assert config_wizard.find_config_drift(cfg)["undeclared"] == ["monitoring.retired_option"]
    assert config_wizard.find_stale_keys(cfg)["monitoring"] == ["retired_option"]


def test_a_key_declared_in_the_example_but_absent_from_config_is_reported() -> None:
    """The other direction: running on a fallback default nobody chose."""
    cfg = _example_as_a_users_config()
    cfg.remove_option("homebrew", "homebrew_tap_repo")

    drift = config_wizard.find_config_drift(cfg)
    assert drift["missing"] == ["homebrew.homebrew_tap_repo"]
    assert drift["undeclared"] == []


def test_a_whole_missing_section_is_reported_key_by_key() -> None:
    """A config.ini that never had `[pypi]` is missing its keys, not a section."""
    cfg = _example_as_a_users_config()
    cfg.remove_section("pypi")

    assert config_wizard.find_config_drift(cfg)["missing"] == ["pypi.pypi_token"]


def test_an_uppercase_spelling_on_disk_is_not_reported_as_drift(tmp_path: Path) -> None:
    """The owner writes `SLACK_BOT_TOKEN`; the example declares `slack_bot_token` (#3947).

    `config.load_config` lowercases option names on read, so a raw-spelling
    comparison would report a live, correctly-set credential as undeclared --
    and the resolution the dashboard offers for an undeclared key is "remove".
    """
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[monitoring]\nSLACK_BOT_TOKEN = xoxb-uppercase-on-disk\n", "utf-8")

    drift = config_wizard.find_config_drift(config.load_config(str(cfg_path)))
    assert drift["undeclared"] == []


def test_the_p004_keys_are_never_reported_as_drift() -> None:
    """Ledger P-004: reporting them invites exactly the wrong fix.

    They are real keys in the owner's config.ini, deliberately undeclared, and
    deliberately not to be removed. An "undeclared key" report on them steers
    toward deleting nyxAgent's groundwork.
    """
    cfg = _example_as_a_users_config()
    cfg.set("github", "qa_agent_token", "x")
    cfg.set("github", "gh_token_nyxagent", "y")

    assert config_wizard.find_config_drift(cfg)["undeclared"] == []
    assert sorted(config_wizard.UNDECLARED_BY_DESIGN) == [
        "github.gh_token_nyxagent",
        "github.qa_agent_token",
    ]


def test_the_report_never_contains_a_value() -> None:
    """A drift report about credentials must be safe to paste into an issue."""
    cfg = _example_as_a_users_config()
    cfg.set("github", "hand_added", "ghp_this_value_must_never_appear")

    drift = config_wizard.find_config_drift(cfg)
    assert "ghp_this_value_must_never_appear" not in repr(drift)
