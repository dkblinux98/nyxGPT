"""The wizard's `secret` flag must agree with every other reader of the same key (#3947).

`config_wizard.WIZARD_SCHEMA` is derived from `example.config.ini`, which
carries no notion of sensitivity: a field is a secret only because
`_FIELD_OVERRIDES` says so, and a field nobody remembered to list there
defaults to `secret=False`. That default is not neutral -- `read_sections`
branches on the flag, so a missed entry means
`GET /api/v1/config/sections` returns the value **in cleartext** to every
browser that opens the Configuration Wizard, on every load, with the value
then held in page state and posted back verbatim on save.

That is exactly what happened to `[monitoring] slack_bot_token`: three other
readers in this codebase already classified it as sensitive
(`config.SECRETS_SYNC_MANIFEST` pushes it to a GitHub Actions **secret**,
`get_effective_config_summary` redacts it, `example.config.ini` documents it
as write-once) and the wizard alone did not.

So the guard here is deliberately **general** rather than naming that field:
it derives the set of keys this codebase already treats as sensitive from
those readers, and fails if any of them is wizard-editable without
`secret=True`. A test naming `slack_bot_token` would pass forever while the
next added credential repeats the defect.

The companion guard, `test_read_sections_never_returns_cleartext_for_a_secret`,
pins `read_sections`'s own docstring claim ("Secret fields are never returned
in cleartext") against the schema rather than leaving it as prose.
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path

import pytest

from nyxgpt import config, config_wizard, secrets_setup

pytestmark = pytest.mark.unit

_EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "example.config.ini"

_SENTINEL = "sentinel-secret-value"


def _example_parser() -> ConfigParser:
    """Parse `example.config.ini` preserving key case, as the wizard does."""
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")
    return parser


def _fully_populated_config() -> ConfigParser:
    """Return a config with *every* example option set to something non-empty.

    Options the example file leaves blank (which is every credential, by
    design) get a sentinel; options with a real example value keep it, so
    typed getters (`getboolean`/`getfloat`) still parse. This is what lets
    `get_effective_config_summary` be *asked* which keys it redacts instead
    of that list being copied here by hand.
    """
    parser = _example_parser()
    for section in parser.sections():
        for key in parser.options(section):
            if not parser.get(section, key).strip():
                parser.set(section, key, _SENTINEL)
    return parser


def _keys_redacted_by_the_config_summary() -> set[str]:
    """Return the `section.key` names `get_effective_config_summary` redacts."""
    summary = config.get_effective_config_summary(_fully_populated_config())
    return {name for name, value in summary.items() if value == config._REDACTED}


def _sensitive_keys() -> set[str]:
    """Every `section.key` this codebase already treats as a secret, from its own readers."""
    return (
        set(config.SECRETS_SYNC_MANIFEST)
        | _keys_redacted_by_the_config_summary()
        | {s.full_key for s in secrets_setup.GUIDED_SECRETS}
    )


def _wizard_fields() -> dict[str, config_wizard.FieldSpec]:
    """Return every wizard-editable field, keyed `section.key`."""
    return {
        f"{spec.section}.{field.key}": field
        for spec in config_wizard.WIZARD_SCHEMA
        for field in spec.fields
    }


def test_the_sensitivity_sources_are_all_still_readable() -> None:
    """Guard the guard: an empty source set would make the real test vacuous.

    Each of the three readers is a separate structure that could be renamed
    or emptied; if that happened silently, the test below would pass by
    checking nothing.
    """
    assert config.SECRETS_SYNC_MANIFEST
    assert _keys_redacted_by_the_config_summary()
    assert {s.full_key for s in secrets_setup.GUIDED_SECRETS}


def test_no_wizard_field_this_codebase_calls_a_secret_is_declared_secret_false() -> None:
    """The general form of #3947: sensitivity is one decision, made once.

    Any key pushed as a GitHub Actions secret, redacted in the effective
    config summary, or walked by the guided secrets flow must be `secret=True`
    wherever the wizard also exposes it. Keys in `EXCLUDED_SECTIONS`
    (`openai`, `github`, `cloud`, `paths`) are not wizard-editable at all and
    so are not covered here -- they cannot leak through an endpoint that
    never returns them.
    """
    fields = _wizard_fields()
    mismatches = sorted(
        full_key
        for full_key in _sensitive_keys()
        if full_key in fields and not fields[full_key].secret
    )
    assert mismatches == [], (
        "wizard-editable fields treated as secrets elsewhere but declared "
        f"secret=False (they would be returned in cleartext by "
        f"GET /api/v1/config/sections): {mismatches}"
    )


def test_monitoring_slack_bot_token_is_the_field_that_regressed() -> None:
    """The specific case #3947 was filed for, kept alongside the general rule.

    The general test above is what catches the *next* one; this one names the
    field so a future edit that drops the override fails with the issue in the
    failure message rather than as an anonymous set difference.
    """
    assert _wizard_fields()["monitoring.slack_bot_token"].secret is True


def test_read_sections_never_returns_cleartext_for_a_secret() -> None:
    """Pin `read_sections`'s docstring claim to the schema, not to prose.

    Every secret field is set to a distinct, recognisable value; the whole
    serialized response must then contain none of those values, and each
    secret must come back as the `{set, masked}` pair the wizard UI renders.
    """
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    planted: dict[str, str] = {}
    for spec in config_wizard.WIZARD_SCHEMA:
        parser.add_section(spec.section)
        for field in spec.fields:
            if not field.secret:
                continue
            value = f"cleartext-{spec.section}-{field.key}-0123456789"
            planted[f"{spec.section}.{field.key}"] = value
            parser.set(spec.section, field.key, value)

    assert planted, "no secret fields in the schema -- this test would be vacuous"

    sections = config_wizard.read_sections(parser)
    serialized = json.dumps(sections)

    for full_key, value in planted.items():
        section, key = full_key.split(".", 1)
        entry = sections[section][key]
        assert isinstance(entry, dict), f"{full_key} came back as a bare value"
        assert set(entry) == {"set", "masked"}
        assert entry["set"] is True
        assert entry["masked"] != value
        assert value not in serialized, f"{full_key} was returned in cleartext"


def test_mask_applied_masks_every_secret_the_save_response_reflects_back() -> None:
    """The save response is the other direction the same value can leak in."""
    applied = {
        spec.section: {
            field.key: f"rotated-{spec.section}-{field.key}-0123456789"
            for field in spec.fields
            if field.secret
        }
        for spec in config_wizard.WIZARD_SCHEMA
    }
    applied = {section: fields for section, fields in applied.items() if fields}

    masked = config_wizard.mask_applied(applied)
    serialized = json.dumps(masked)
    for section, fields in applied.items():
        for key, value in fields.items():
            assert masked[section][key] == {
                "set": True,
                "masked": config_wizard._mask(value),
            }
            assert value not in serialized


def test_a_blank_submission_of_a_secret_leaves_the_on_disk_value_untouched(
    tmp_path: Path,
) -> None:
    """Flipping a field to `secret=True` must not break an existing user (#3947).

    The wizard no longer echoes the value back, so the browser posts an empty
    string for a field the user did not retype. `validate_updates` drops it,
    which is what keeps a plain "Save" from blanking a live token.
    """
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[monitoring]\n"
        "enabled = true\n"
        "slack_bot_token = xoxb-real-token-from-slack\n"
        "grafana_admin_password = hunter2hunter2\n",
        encoding="utf-8",
    )

    validated, errors = config_wizard.validate_updates(
        {"monitoring": {"slack_bot_token": "", "grafana_admin_password": "  "}}
    )
    assert errors == []
    assert validated == {}

    config_wizard.apply_updates(cfg_path, validated)
    assert "xoxb-real-token-from-slack" in cfg_path.read_text(encoding="utf-8")
    assert "hunter2hunter2" in cfg_path.read_text(encoding="utf-8")


def test_a_nonblank_submission_of_a_secret_still_rotates_it(tmp_path: Path) -> None:
    """The other half of the rule: a typed value is a rotation, not a no-op."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[monitoring]\nslack_bot_token = xoxb-old-token\n",
        encoding="utf-8",
    )

    validated, errors = config_wizard.validate_updates(
        {"monitoring": {"slack_bot_token": "xoxb-new-token"}}
    )
    assert errors == []
    assert validated == {"monitoring": {"slack_bot_token": "xoxb-new-token"}}

    config_wizard.apply_updates(cfg_path, validated)
    text = cfg_path.read_text(encoding="utf-8")
    assert "xoxb-new-token" in text
    assert "xoxb-old-token" not in text
