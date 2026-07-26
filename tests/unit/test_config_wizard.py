"""Unit tests for src/nyxgpt/config_wizard.py (#3354).

Covers schema validation, restart/observability change detection, and the
read/apply file round-trip -- the pure logic backing the full Configuration
Wizard's `GET|POST /api/v1/config/sections` endpoints.
"""

from __future__ import annotations

from configparser import ConfigParser

import pytest

from nyxgpt import config_wizard

pytestmark = pytest.mark.unit


def _cfg(**sections: dict[str, str]) -> ConfigParser:
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    for section, values in sections.items():
        parser.add_section(section)
        for key, value in values.items():
            parser.set(section, key, value)
    return parser


# --- validate_updates ---


def test_validate_updates_accepts_known_fields():
    validated, errors = config_wizard.validate_updates(
        {"nyxgpt": {"default_model": "llama3"}, "api": {"port": "9000"}}
    )
    assert errors == []
    assert validated == {"nyxgpt": {"default_model": "llama3"}, "api": {"port": 9000}}


def test_validate_updates_rejects_unknown_section():
    validated, errors = config_wizard.validate_updates({"bogus": {"x": "1"}})
    assert validated == {}
    assert "bogus: unknown section" in errors


def test_validate_updates_rejects_unknown_field():
    validated, errors = config_wizard.validate_updates({"nyxgpt": {"bogus_key": "1"}})
    assert validated == {}
    assert "nyxgpt.bogus_key: unknown field" in errors


def test_validate_updates_rejects_bad_port():
    validated, errors = config_wizard.validate_updates({"api": {"port": 70000}})
    assert validated == {}
    assert any("api.port" in e for e in errors)


def test_validate_updates_rejects_bad_url():
    validated, errors = config_wizard.validate_updates({"ollama": {"base_url": "not-a-url"}})
    assert validated == {}
    assert any("ollama.base_url" in e for e in errors)


def test_validate_updates_rejects_bad_log_level():
    validated, errors = config_wizard.validate_updates({"logging": {"level": "VERBOSE"}})
    assert validated == {}
    assert any("logging.level" in e for e in errors)


def test_validate_updates_normalizes_log_level_case():
    validated, errors = config_wizard.validate_updates({"logging": {"level": "debug"}})
    assert errors == []
    assert validated == {"logging": {"level": "DEBUG"}}


def test_validate_updates_empty_secret_is_dropped_not_error():
    validated, errors = config_wizard.validate_updates({"auth": {"api_key": "", "enabled": True}})
    assert errors == []
    assert validated == {"auth": {"enabled": True}}


def test_validate_updates_nonempty_secret_is_kept():
    validated, errors = config_wizard.validate_updates({"auth": {"api_key": "s3cr3t"}})
    assert errors == []
    assert validated == {"auth": {"api_key": "s3cr3t"}}


def test_validate_updates_rejects_non_object_payload():
    validated, errors = config_wizard.validate_updates(["not", "a", "dict"])
    assert validated == {}
    assert errors


def test_validate_updates_rejects_non_object_section():
    validated, errors = config_wizard.validate_updates({"nyxgpt": "not-a-dict"})
    assert validated == {}
    assert any("nyxgpt: must be an object" in e for e in errors)


def test_validate_updates_host_list_normalizes_whitespace():
    validated, errors = config_wizard.validate_updates(
        {"rag": {"cassandra_hosts": " 10.0.0.1 , 10.0.0.2 "}}
    )
    assert errors == []
    assert validated == {"rag": {"cassandra_hosts": "10.0.0.1, 10.0.0.2"}}


def test_validate_updates_rejects_empty_host_list():
    validated, errors = config_wizard.validate_updates({"rag": {"cassandra_hosts": "  ,  "}})
    assert validated == {}
    assert any("rag.cassandra_hosts" in e for e in errors)


# --- restart_components ---


def test_restart_components_flags_changed_api_port():
    cfg = _cfg(api={"host": "127.0.0.1", "port": "8000"})
    validated = {"api": {"port": 9000}}
    assert config_wizard.restart_components(validated, cfg) == ["api"]


def test_restart_components_ignores_unchanged_value():
    cfg = _cfg(api={"host": "127.0.0.1", "port": "8000"})
    validated = {"api": {"port": 8000}}
    assert config_wizard.restart_components(validated, cfg) == []


def test_restart_components_ignores_hot_fields():
    cfg = _cfg(nyxgpt={"default_model": "a"})
    validated = {"nyxgpt": {"default_model": "b"}}
    assert config_wizard.restart_components(validated, cfg) == []


def test_restart_components_dedupes_across_sections():
    cfg = _cfg(
        api={"host": "127.0.0.1", "port": "8000"},
        rag={"embedding_model": "old"},
    )
    validated = {"api": {"port": 9000}, "rag": {"embedding_model": "new"}}
    assert config_wizard.restart_components(validated, cfg) == ["api"]


# --- observability_changed ---


def test_observability_changed_true_on_enable():
    cfg = _cfg(tracing={"enabled": "false"})
    validated = {"tracing": {"enabled": True}}
    assert config_wizard.observability_changed(validated, cfg) is True


def test_observability_changed_false_when_unchanged():
    cfg = _cfg(tracing={"enabled": "true"})
    validated = {"tracing": {"enabled": True}}
    assert config_wizard.observability_changed(validated, cfg) is False


def test_observability_changed_false_for_non_observability_field():
    cfg = _cfg(tracing={"service_name": "old"})
    validated = {"tracing": {"service_name": "new"}}
    assert config_wizard.observability_changed(validated, cfg) is False


# --- read_sections ---


def test_read_sections_masks_secrets():
    cfg = _cfg(auth={"api_key": "abcdefghijklmnop"})
    sections = config_wizard.read_sections(cfg)
    assert sections["auth"]["api_key"]["set"] is True
    assert sections["auth"]["api_key"]["masked"] == "abcd********mnop"


def test_read_sections_unset_secret():
    cfg = _cfg()
    sections = config_wizard.read_sections(cfg)
    assert sections["auth"]["api_key"] == {"set": False, "masked": None}


def test_read_sections_plain_field_passthrough():
    cfg = _cfg(nyxgpt={"default_model": "llama3"})
    sections = config_wizard.read_sections(cfg)
    assert sections["nyxgpt"]["default_model"] == "llama3"


# --- apply_updates ---


def test_apply_updates_writes_and_preserves_existing(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = keep-me\n\n[unrelated]\nfoo = bar\n")

    applied = config_wizard.apply_updates(cfg_path, {"logging": {"level": "DEBUG"}})

    assert applied == {"logging": {"level": "DEBUG"}}
    written = cfg_path.read_text()
    parser = ConfigParser()
    parser.read_string(written)
    assert parser.get("nyxgpt", "default_model") == "keep-me"
    assert parser.get("unrelated", "foo") == "bar"
    assert parser.get("logging", "level") == "DEBUG"


def test_apply_updates_writes_bool_as_lowercase_string(tmp_path):
    cfg_path = tmp_path / "config.ini"
    config_wizard.apply_updates(cfg_path, {"auth": {"enabled": True}})

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("auth", "enabled") == "true"


def test_apply_updates_creates_file_when_missing(tmp_path):
    cfg_path = tmp_path / "nested" / "config.ini"
    applied = config_wizard.apply_updates(cfg_path, {"api": {"port": 9000}})
    assert applied == {"api": {"port": 9000}}
    assert cfg_path.exists()


def test_schema_summary_covers_every_section():
    summary = config_wizard.schema_summary()
    sections = {s["section"] for s in summary}
    assert sections == {
        "nyxgpt",
        "logging",
        "ollama",
        "api",
        "auth",
        "rate_limit",
        "rag",
        "tracing",
        "error_tracking",
        "monitoring",
        "log_aggregation",
    }
