"""Activation classification, pending-restart state, and the surfaces that report it (#3806).

The defect this file guards against: `[auth] api_key` was hot on the `api`
tier (`require_api_key` re-reads config every request) and frozen on the `web`
tier (the service wrapper exports `NYXGPT_AUTH_API_KEY` once at process start),
with nothing in the product acknowledging the difference. Rotating the key --
from the Configuration Wizard, from `nyxgpt secrets setup`, or by hand -- left
the web UI sending the old key into a 401 wall, silently, including in the very
session that did the rotating.

The tests below cover the general mechanism rather than that one key:

* `TestClassificationIsData` -- the classification exists as data, is
  reachable by non-wizard writers, and `example.config.ini`'s annotations
  match it exactly.
* `TestWrapperReadKeysAreClassified` -- **the regression guard**. It
  re-derives, from `ops.py`'s own service-wrapper templates, the set of config
  keys each tier reads once at process start, and fails if any of them is not
  classified restart-required for that tier. This is what makes the next
  wrapper-read key impossible to add silently.
* `TestPendingState` -- the state is on disk (so the CLI writer and the API
  reader see one set), survives a process boundary, and retires itself on
  either a restart or a revert.
* `TestSurfaces` -- the CLI notice names the service and the wrapped command.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from configparser import ConfigParser
from pathlib import Path

import pytest

from nyxgpt import config_wizard, restart_state, secrets_setup

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "example.config.ini"

# Matches the generated annotation line in example.config.ini, e.g.
#   # Activation: restart required (web) -- nyxgpt ops restart web
_ANNOTATION_RE = re.compile(r"^#\s*Activation:\s*restart required\s*\(([^)]+)\)")
_KEY_RE = re.compile(r"^([A-Za-z0-9_]+)\s*=")
_SECTION_RE = re.compile(r"^\[(.+)\]\s*$")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point pending-restart state at a temp file so tests never touch ~/.nyxGPT."""
    monkeypatch.setenv("NYXGPT_PENDING_RESTART_PATH", str(tmp_path / "pending-restart.json"))
    yield
    restart_state.reset()


def _annotated_keys() -> dict[str, tuple[str, ...]]:
    """Parse `example.config.ini` into `{"section.key": annotated components}`.

    Only keys carrying an `# Activation: restart required (...)` line
    immediately above them appear; everything else in the file is, by the
    file's own stated convention, hot-reloadable.
    """
    out: dict[str, tuple[str, ...]] = {}
    section = None
    pending: tuple[str, ...] | None = None
    for line in EXAMPLE_CONFIG.read_text(encoding="utf-8").splitlines():
        sm = _SECTION_RE.match(line)
        if sm:
            section, pending = sm.group(1), None
            continue
        am = _ANNOTATION_RE.match(line)
        if am:
            pending = tuple(part.strip() for part in am.group(1).split(","))
            continue
        km = _KEY_RE.match(line)
        if km and section and pending:
            out[f"{section}.{km.group(1)}"] = pending
        pending = None
    return out


class TestClassificationIsData:
    """The classification is data the product acts on, not prose in a config file."""

    def test_auth_api_key_is_restart_required_for_web(self):
        """The worked example from #3806: rotating it leaves `web` stale."""
        assert config_wizard.field_restart_components("auth", "api_key") == ("web",)

    def test_hot_reloadable_keys_classify_as_empty(self):
        """A genuinely hot key must not raise a notice -- that would train users to ignore it."""
        assert config_wizard.field_restart_components("nyxgpt", "default_model") == ()
        assert config_wizard.field_restart_components("logging", "level") == ()

    def test_lookup_is_available_to_non_wizard_writers(self):
        """`secrets_setup` and any future writer must classify through one shared function."""
        assert config_wizard.field_restart_components("web", "port") == ("web",)
        # A section the wizard excludes has no running consumer to go stale.
        assert config_wizard.field_restart_components("openai", "api_key") == ()

    def test_schema_summary_exposes_the_classification_to_the_ui(self):
        """The UI renders the backend's classification, so the two can't disagree."""
        auth = next(s for s in config_wizard.schema_summary() if s["section"] == "auth")
        api_key = next(f for f in auth["fields"] if f["key"] == "api_key")
        assert api_key["restart_components"] == ["web"]

    def test_example_config_annotations_match_the_classification_exactly(self):
        """`example.config.ini`'s annotations are generated from the classification.

        This is the acceptance criterion "keep the example config's
        annotations in sync with it": drift in either direction fails.
        """
        classified = {
            key: components
            for key, components in config_wizard.activation_classification().items()
            if components
        }
        assert _annotated_keys() == classified

    def test_no_key_is_annotated_both_hot_and_restart_required(self):
        """The pre-existing "(hot-reload; no restart required)" prose must not contradict it.

        Checks the *classification*, not the neighbouring line: a key whose
        comment block promises a hot reload must classify as hot. Comparing
        against `activation_classification()` is what makes this fail on the
        realistic drift -- someone classifying a key restart-required and
        leaving the old promise in the file above it.
        """
        classified = config_wizard.activation_classification()
        lines = EXAMPLE_CONFIG.read_text(encoding="utf-8").splitlines()
        section: str | None = None
        promised_hot = False
        checked = 0
        for line in lines:
            sm = _SECTION_RE.match(line)
            if sm:
                section, promised_hot = sm.group(1), False
                continue
            if "hot-reload; no restart required" in line:
                promised_hot = True
                continue
            km = _KEY_RE.match(line)
            if km and section and promised_hot:
                full_key = f"{section}.{km.group(1)}"
                assert not classified.get(full_key), (
                    f"{full_key} promises a hot reload in example.config.ini but is "
                    f"classified restart-required for {classified[full_key]}"
                )
                checked += 1
            if km or (line.strip() and not line.lstrip().startswith("#")):
                promised_hot = False
        # The prose is what this guards; if it ever disappears entirely the
        # test would pass vacuously, so require that it found some.
        assert checked > 0, "no 'hot-reload; no restart required' prose found to check"


class TestWrapperReadKeysAreClassified:
    """Regression guard: a key read once at process start must be classified for that tier.

    `ops.py`'s wrapper templates are the authoritative statement of what each
    native service reads from config.ini before it starts. Anything read there
    is, by construction, frozen for that process's lifetime -- so if it is not
    classified restart-required for that service, the product will apply it
    silently on the other tier and never on this one. That is exactly the
    `[auth] api_key` failure, expressed generally.
    """

    @staticmethod
    def _keys_read_by(template: str) -> set[tuple[str, str]]:
        """Extract every `cfg.get*('section', 'key'...)` pair from a wrapper template."""
        return {
            (m.group(1), m.group(2))
            for m in re.finditer(
                r"cfg\.get(?:boolean|int|float)?\(\s*'([^']+)'\s*,\s*'([^']+)'", template
            )
        }

    def test_web_wrapper_keys_are_all_restart_required_for_web(self):
        from nyxgpt import ops

        read = self._keys_read_by(ops._NATIVE_WEB_WRAPPER_TEMPLATE)
        # Sanity: the extraction actually found the auth key, so a template
        # rewrite that breaks the regex fails loudly instead of passing empty.
        assert ("auth", "api_key") in read

        unclassified = [
            f"{section}.{key}"
            for section, key in sorted(read)
            if "web" not in config_wizard.field_restart_components(section, key)
        ]
        assert not unclassified, (
            "These keys are read once at start by the web service wrapper but are not "
            f"classified restart-required for 'web': {unclassified}. A user changing them "
            "would see the api tier apply the change and the web tier silently keep the old "
            "value, with no pending-restart notice (#3806)."
        )

    def test_api_wrapper_keys_are_all_restart_required_for_api(self):
        from nyxgpt import ops

        read = self._keys_read_by(ops._NATIVE_API_WRAPPER_TEMPLATE)
        assert ("api", "host") in read

        unclassified = [
            f"{section}.{key}"
            for section, key in sorted(read)
            if "api" not in config_wizard.field_restart_components(section, key)
        ]
        assert not unclassified, (
            "These keys are read once at start by the api service wrapper but are not "
            f"classified restart-required for 'api': {unclassified} (#3806)."
        )


class TestPendingState:
    """Pending-restart state is durable, cross-process, and self-retiring."""

    def test_mark_and_snapshot(self):
        restart_state.mark_pending("web", {"auth.api_key": "old-key"})
        snap = restart_state.snapshot()
        assert snap["web"]["keys"] == ["auth.api_key"]
        assert snap["web"]["since"] > 0

    def test_snapshot_never_leaks_the_previous_value(self):
        """Several classified keys are secrets; the notice needs names, not values."""
        restart_state.mark_pending("web", {"auth.api_key": "super-secret-old-key"})
        assert "super-secret-old-key" not in json.dumps(restart_state.snapshot())

    def test_state_is_visible_to_another_process(self, tmp_path, monkeypatch):
        """The CLI writes it and the API reads it -- an in-memory flag cannot do this.

        This is the property the #3407 in-memory implementation lacked, and
        the reason `nyxgpt secrets setup` could not raise the notice at all.
        """
        state_file = tmp_path / "cross-process.json"
        monkeypatch.setenv("NYXGPT_PENDING_RESTART_PATH", str(state_file))
        restart_state.mark_pending("web", {"auth.api_key": "old-key"})

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from nyxgpt import restart_state; import json; "
                "print(json.dumps(restart_state.snapshot()))",
            ],
            capture_output=True,
            text=True,
            env={
                "NYXGPT_PENDING_RESTART_PATH": str(state_file),
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
            check=True,
        )
        assert json.loads(result.stdout)["web"]["keys"] == ["auth.api_key"]

    def test_a_second_edit_keeps_the_original_running_value(self):
        """The running value doesn't change until the restart, so it must not be overwritten."""
        restart_state.mark_pending("web", {"auth.api_key": "running-value"})
        restart_state.mark_pending("web", {"auth.api_key": "intermediate-value"})
        # Reverting to the *running* value clears it...
        restart_state.reconcile_saved("web", {"auth.api_key": "running-value"})
        assert restart_state.snapshot() == {}

    def test_reverting_retires_the_notice_without_a_restart(self):
        restart_state.mark_pending("web", {"auth.api_key": "old", "web.port": "3000"})
        restart_state.reconcile_saved("web", {"auth.api_key": "old"})
        assert restart_state.snapshot()["web"]["keys"] == ["web.port"]

    def test_a_different_new_value_does_not_retire_the_notice(self):
        restart_state.mark_pending("web", {"web.port": "3000"})
        restart_state.reconcile_saved("web", {"web.port": "3002"})
        assert restart_state.snapshot()["web"]["keys"] == ["web.port"]

    def test_clear_pending_after_restart(self):
        restart_state.mark_pending("web", {"auth.api_key": "old"})
        restart_state.clear_pending("web")
        assert restart_state.snapshot() == {}

    def test_restart_command_is_always_the_wrapped_command(self):
        """Never a raw brew/docker/kubectl command (the operational-wrapping rule)."""
        assert restart_state.restart_command(["web"]) == "nyxgpt ops restart web"
        assert restart_state.restart_command(["web", "api"]) == (
            "nyxgpt ops restart api && nyxgpt ops restart web"
        )

    def test_corrupt_state_degrades_to_nothing_pending(self, tmp_path, monkeypatch):
        """Advisory UI state must never break a config save or an API response."""
        state_file = tmp_path / "corrupt.json"
        state_file.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("NYXGPT_PENDING_RESTART_PATH", str(state_file))
        assert restart_state.snapshot() == {}


class TestWizardDetail:
    """`restart_required_detail` records the running value so a revert can be recognised."""

    @staticmethod
    def _cfg(**auth) -> ConfigParser:
        cfg = ConfigParser()
        cfg.read_dict({"auth": {"enabled": "true", "api_key": "old-key", **auth}})
        return cfg

    def test_records_previous_value_per_component(self):
        detail = config_wizard.restart_required_detail(
            {"auth": {"api_key": "new-key"}}, self._cfg()
        )
        assert detail == {"web": {"auth.api_key": "old-key"}}

    def test_unchanged_value_is_not_pending(self):
        assert (
            config_wizard.restart_required_detail({"auth": {"api_key": "old-key"}}, self._cfg())
            == {}
        )

    def test_saved_map_covers_unchanged_keys_too(self):
        """`reconcile_saved` needs the new value of every restart-required key in the payload."""
        saved = config_wizard.restart_activation_saved({"auth": {"api_key": "old-key"}})
        assert saved == {"web": {"auth.api_key": "old-key"}}


class TestSurfaces:
    """One behavior, two surfaces: the CLI says what the wizard says."""

    def test_cli_notice_names_the_service_and_the_command(self):
        spec = secrets_setup.find_guided_secret("auth", "api_key")
        assert spec is not None
        notice = secrets_setup.restart_notice(spec)
        assert notice is not None
        assert "NOT YET IN EFFECT" in notice
        assert "web" in notice
        assert "nyxgpt ops restart web" in notice

    def test_cli_notice_is_absent_for_hot_secrets(self):
        """`[github] pat` has no long-lived process holding a stale copy."""
        spec = secrets_setup.find_guided_secret("github", "pat")
        assert spec is not None
        assert secrets_setup.restart_notice(spec) is None

    def test_writing_the_auth_key_from_the_cli_raises_the_pending_notice(self, tmp_path):
        """The CLI path marks the same state the Admin Dashboard polls."""
        cfg_path = tmp_path / "config.ini"
        cfg_path.write_text("[auth]\napi_key = old-key\n", encoding="utf-8")
        spec = secrets_setup.find_guided_secret("auth", "api_key")
        assert spec is not None

        secrets_setup.write_secret(cfg_path, spec, "rotated-key-value")

        assert restart_state.snapshot()["web"]["keys"] == ["auth.api_key"]

    def test_cli_revert_retires_the_notice(self, tmp_path):
        cfg_path = tmp_path / "config.ini"
        cfg_path.write_text("[auth]\napi_key = old-key\n", encoding="utf-8")
        spec = secrets_setup.find_guided_secret("auth", "api_key")
        assert spec is not None

        secrets_setup.write_secret(cfg_path, spec, "rotated-key-value")
        secrets_setup.write_secret(cfg_path, spec, "old-key")

        assert restart_state.snapshot() == {}
