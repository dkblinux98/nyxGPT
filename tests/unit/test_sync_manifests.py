"""The two sync manifests must stay complete, disjoint, and on the right side of the line (#3976).

`config.ini` is documented as the canonical store for write-once external
credentials (#3505) and `nyxgpt ops config-sync` as the only supported way to
get them into GitHub Actions. Neither claim held: there was no variables push
at all (the 2026-02 shell script that did it was deleted and never replaced),
and `SECRETS_SYNC_MANIFEST` trailed the secrets the workflows actually read by
seven. Extending a manifest was documented as "a one-line addition", which is
precisely why it fell behind -- *nothing failed when it did*.

So the tests here are reconciliation against the workflows themselves rather
than assertions about a list somebody has to remember to update:

* `test_every_actions_variable_a_workflow_reads_is_in_the_variables_manifest`
  and its secrets counterpart parse `.github/workflows/*.yml` for `vars.NAME`
  and `secrets.NAME` and fail on anything no manifest carries.
* `test_no_key_this_codebase_calls_a_secret_is_in_the_variables_manifest` is
  the load-bearing one. A repository *variable* is readable by anyone with
  read access to the repo; a secret is not. It derives the sensitive key set
  the way `test_wizard_secret_classification.py` does -- sync manifest,
  config-summary redactions, `GUIDED_SECRETS` -- and fails if any of them
  appears on the variables side. The deleted script defended this asymmetry
  with a comment above its allowlist. A comment is not a guard.
"""

from __future__ import annotations

import re
from configparser import ConfigParser
from pathlib import Path

import pytest

from nyxgpt import config, secrets_setup

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_EXAMPLE_CONFIG_PATH = _REPO_ROOT / "example.config.ini"

_VARS_RE = re.compile(r"\bvars\.([A-Z][A-Z0-9_]*)")
_SECRETS_RE = re.compile(r"\bsecrets\.([A-Z][A-Z0-9_]*)")

#: Actions secrets a workflow reads that `config.ini` deliberately does not
#: carry. `GITHUB_TOKEN` is minted per-run by Actions itself -- there is
#: nothing to push and no way to push it.
_SECRETS_NOT_FROM_CONFIG = frozenset({"GITHUB_TOKEN"})

#: Repository variables a workflow reads that `config.ini` deliberately does
#: not carry. Empty today: every `vars.` reference in the tree has a key.
#: Adding a name here is a decision that needs a reason next to it, which is
#: the point of making the omission explicit rather than implicit.
_VARIABLES_NOT_FROM_CONFIG: frozenset[str] = frozenset()

_SENTINEL = "sentinel-secret-value"


def _workflow_text() -> str:
    """Concatenate every workflow file, so one regex sweep covers the tree."""
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_WORKFLOWS_DIR.glob("*.yml"))
    )


def _example_parser() -> ConfigParser:
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")
    return parser


def _fully_populated_config() -> ConfigParser:
    """Every example option set to something non-empty (see test_wizard_secret_classification)."""
    parser = _example_parser()
    for section in parser.sections():
        for key in parser.options(section):
            if not parser.get(section, key).strip():
                parser.set(section, key, _SENTINEL)
    return parser


def _keys_redacted_by_the_config_summary() -> set[str]:
    summary = config.get_effective_config_summary(_fully_populated_config())
    return {name for name, value in summary.items() if value == config._REDACTED}


def _sensitive_keys() -> set[str]:
    """Every `section.key` this codebase already treats as a secret, from its own readers."""
    return (
        set(config.SECRETS_SYNC_MANIFEST)
        | _keys_redacted_by_the_config_summary()
        | {s.full_key for s in secrets_setup.GUIDED_SECRETS}
    )


# --- The load-bearing guard: a secret can never be pushed as a variable ---


def test_the_sensitivity_sources_are_all_still_readable() -> None:
    """Guard the guard: an empty source set would make the real test vacuous."""
    assert config.SECRETS_SYNC_MANIFEST
    assert _keys_redacted_by_the_config_summary()
    assert {s.full_key for s in secrets_setup.GUIDED_SECRETS}
    assert config.VARIABLES_SYNC_MANIFEST


def test_no_key_this_codebase_calls_a_secret_is_in_the_variables_manifest() -> None:
    """A repository variable is world-readable; a secret is not (#3976).

    Derived from the same three readers `test_wizard_secret_classification.py`
    uses, so a credential classified anywhere is classified here -- naming the
    keys by hand would pass forever while the next added one repeats the
    defect.
    """
    leaked = sorted(_sensitive_keys() & set(config.VARIABLES_SYNC_MANIFEST))
    assert leaked == [], (
        "config.ini keys this codebase treats as secrets are listed in "
        "VARIABLES_SYNC_MANIFEST, which would publish them to anyone with "
        f"read access to the repository: {leaked}"
    )


def test_the_two_manifests_never_claim_the_same_destination_name() -> None:
    """The other way the same value lands in both places: one name, two APIs."""
    shared = sorted(
        set(config.SECRETS_SYNC_MANIFEST.values()) & set(config.VARIABLES_SYNC_MANIFEST.values())
    )
    assert shared == []


def test_the_disjointness_check_actually_fails_on_an_overlap(monkeypatch) -> None:
    """Fault injection: the import-time guard must not be a no-op.

    `config._assert_manifests_are_disjoint` runs once, at import, when the
    manifests are already correct -- so on its own it proves nothing about
    what it would do if they were not. Feed it an overlap and require the
    raise, for the key case and the name case separately.
    """
    monkeypatch.setattr(
        config, "VARIABLES_SYNC_MANIFEST", {"monitoring.slack_bot_token": "SLACK_BOT_TOKEN_VAR"}
    )
    with pytest.raises(RuntimeError, match="world-readable variables API"):
        config._assert_manifests_are_disjoint()

    monkeypatch.setattr(config, "VARIABLES_SYNC_MANIFEST", {"github.repo_owner": "SLACK_BOT_TOKEN"})
    with pytest.raises(RuntimeError, match="both a secret and a variable"):
        config._assert_manifests_are_disjoint()


# --- Completeness: reconcile both manifests against the workflows ---


def test_every_actions_variable_a_workflow_reads_is_in_the_variables_manifest() -> None:
    """The defect in the issue's title, stated as a check.

    Four variables added after the 2026-02 script was deleted
    (`CLAUDE_REVIEW_ENABLED`, `HOMEBREW_TAP_REPO`, `SPRINT_AUTOPILOT`,
    `STATUS_ACCEPTANCE_TESTING`) plus `SLACK_HUDDLE_CHANNEL` were set by hand,
    and nothing anywhere noticed. This is what notices.
    """
    referenced = set(_VARS_RE.findall(_workflow_text()))
    assert referenced, "no vars.* references found -- the regex or the path is wrong"
    uncovered = sorted(
        referenced - set(config.VARIABLES_SYNC_MANIFEST.values()) - _VARIABLES_NOT_FROM_CONFIG
    )
    assert uncovered == [], (
        "GitHub Actions variables the workflows read that config.ini cannot "
        f"populate, so they can only be set by hand: {uncovered}. Add a "
        "config.ini key plus a VARIABLES_SYNC_MANIFEST entry, or list the "
        "name in _VARIABLES_NOT_FROM_CONFIG with the reason."
    )


def test_every_actions_secret_a_workflow_reads_is_in_the_secrets_manifest() -> None:
    """Same reconciliation for the secrets half, which trailed reality by seven."""
    referenced = set(_SECRETS_RE.findall(_workflow_text()))
    assert referenced, "no secrets.* references found -- the regex or the path is wrong"
    uncovered = sorted(
        referenced - set(config.SECRETS_SYNC_MANIFEST.values()) - _SECRETS_NOT_FROM_CONFIG
    )
    assert uncovered == [], (
        "GitHub Actions secrets the workflows read that config.ini cannot "
        f"populate: {uncovered}. Add a config.ini key plus a "
        "SECRETS_SYNC_MANIFEST entry, or list the name in "
        "_SECRETS_NOT_FROM_CONFIG with the reason."
    )


def test_every_manifest_key_is_declared_in_example_config_ini() -> None:
    """A manifest entry whose key nobody declares can never have a value to push.

    This is the other direction of the same reconciliation, and it is what
    keeps `nyxgpt ops config-drift` clean: a manifest key absent from
    `example.config.ini` would be reported as drift by every real config.ini
    that carries it.
    """
    parser = _example_parser()
    declared = {
        f"{section}.{key}".lower()
        for section in parser.sections()
        for key in parser.options(section)
    }
    both = {**config.SECRETS_SYNC_MANIFEST, **config.VARIABLES_SYNC_MANIFEST}
    undeclared = sorted(key for key in both if key.lower() not in declared)
    assert (
        undeclared == []
    ), f"manifest keys with no declaration in example.config.ini: {undeclared}"


def test_the_p004_keys_are_in_neither_manifest() -> None:
    """Ledger P-004: nyxAgent groundwork, not to be synced and not to be removed."""
    for full_key in ("github.qa_agent_token", "github.gh_token_nyxagent"):
        assert full_key not in config.SECRETS_SYNC_MANIFEST
        assert full_key not in config.VARIABLES_SYNC_MANIFEST


def test_manifest_destination_names_are_valid_actions_names() -> None:
    """GitHub rejects a name that is not `[A-Z_][A-Z0-9_]*` or starts with `GITHUB_`.

    Catching this here rather than as a 422 from the API halfway through a
    push, which would leave the repo half-synced.
    """
    both = {**config.SECRETS_SYNC_MANIFEST, **config.VARIABLES_SYNC_MANIFEST}
    for full_key, name in both.items():
        assert re.fullmatch(r"[A-Z_][A-Z0-9_]*", name), f"{full_key} -> invalid name {name!r}"
        assert not name.startswith("GITHUB_"), f"{full_key} -> reserved prefix in {name!r}"
