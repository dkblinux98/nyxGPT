"""Unit tests for src/nyxgpt/config_wizard.py (#3354, #3388).

Covers schema derivation from `example.config.ini`, schema validation,
restart/observability change detection, and the comment-preserving
read/apply file round-trip -- the pure logic backing the full Configuration
Wizard's `GET|POST /api/v1/config/sections` endpoints.
"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest

from nyxgpt import config_wizard

pytestmark = pytest.mark.unit

_EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "example.config.ini"


def _cfg(**sections: dict[str, str]) -> ConfigParser:
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    for section, values in sections.items():
        parser.add_section(section)
        for key, value in values.items():
            parser.set(section, key, value)
    return parser


# --- _resolve_example_config_path (#3406, #3388) ---


def test_resolve_example_config_env_override_wins(monkeypatch, tmp_path):
    custom = tmp_path / "custom.ini"
    custom.write_text("[nyxgpt]\n", encoding="utf-8")
    monkeypatch.setenv("NYXGPT_EXAMPLE_CONFIG", str(custom))
    assert config_wizard._resolve_example_config_path() == custom


def test_resolve_example_config_prefers_package_adjacent(monkeypatch, tmp_path):
    """A venv install (e.g. the self-contained Homebrew keg) ships
    example.config.ini next to the module, so resolution finds it there with no
    env var and no repo root above the package (#3406)."""
    monkeypatch.delenv("NYXGPT_EXAMPLE_CONFIG", raising=False)
    pkg_dir = tmp_path / "site-packages" / "nyxgpt"
    pkg_dir.mkdir(parents=True)
    module = pkg_dir / "config_wizard.py"
    module.write_text("", encoding="utf-8")
    packaged = pkg_dir / "example.config.ini"
    packaged.write_text("[nyxgpt]\n", encoding="utf-8")
    monkeypatch.setattr(config_wizard, "__file__", str(module))
    assert config_wizard._resolve_example_config_path() == packaged.resolve()


def test_resolve_example_config_falls_back_to_repo_root(monkeypatch, tmp_path):
    """Source checkout: no env and no package-adjacent copy -> the repo-root
    file (module at src/nyxgpt/config_wizard.py, so parents[2] is the root)."""
    monkeypatch.delenv("NYXGPT_EXAMPLE_CONFIG", raising=False)
    module = tmp_path / "repo" / "src" / "nyxgpt" / "config_wizard.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    # deliberately no example.config.ini next to the module
    monkeypatch.setattr(config_wizard, "__file__", str(module))
    assert (
        config_wizard._resolve_example_config_path()
        == module.resolve().parents[2] / "example.config.ini"
    )


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


def test_read_sections_returns_effective_default_when_section_absent():
    """A config with no [tracing] section at all must show the runtime fallback (#3385)."""
    cfg = _cfg(ollama={"base_url": "http://localhost:11434"})
    sections = config_wizard.read_sections(cfg)
    assert sections["tracing"]["service_name"] == "nyxgpt-api"
    assert sections["tracing"]["otlp_endpoint"] == "http://localhost:4318/v1/traces"
    assert sections["tracing"]["enabled"] == "false"


def test_read_sections_returns_effective_default_for_error_tracking():
    cfg = _cfg()
    sections = config_wizard.read_sections(cfg)
    assert sections["error_tracking"]["environment"] == "development"


def test_read_sections_explicit_value_overrides_default():
    cfg = _cfg(tracing={"service_name": "my-custom-service"})
    sections = config_wizard.read_sections(cfg)
    assert sections["tracing"]["service_name"] == "my-custom-service"


def test_read_sections_field_with_no_fixed_default_is_genuinely_empty():
    """rag.embedding_model has no fixed default (falls back to default_model dynamically)."""
    cfg = _cfg()
    sections = config_wizard.read_sections(cfg)
    assert sections["rag"]["embedding_model"] == ""


# --- field_defaults ---


def test_field_defaults_true_when_key_absent():
    cfg = _cfg()
    defaults = config_wizard.field_defaults(cfg)
    assert defaults["tracing"]["service_name"] is True
    assert defaults["error_tracking"]["environment"] is True


def test_field_defaults_false_when_key_explicit():
    cfg = _cfg(tracing={"service_name": "my-service"})
    defaults = config_wizard.field_defaults(cfg)
    assert defaults["tracing"]["service_name"] is False


def test_field_defaults_excludes_secret_fields():
    cfg = _cfg()
    defaults = config_wizard.field_defaults(cfg)
    assert "api_key" not in defaults["auth"]
    assert "dsn" not in defaults["error_tracking"]


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


def test_schema_summary_covers_every_non_excluded_section():
    """Guards against #3388's original bug: the wizard silently only covering a subset."""
    parser = ConfigParser()
    parser.optionxform = str
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")

    summary = config_wizard.schema_summary()
    sections = {s["section"] for s in summary}
    assert sections == set(parser.sections()) - config_wizard.EXCLUDED_SECTIONS


def test_wizard_schema_never_silently_diverges_from_example_config():
    """A section added to example.config.ini must be rendered or explicitly excluded (#3388).

    This is the regression test the issue asked for: it re-parses
    example.config.ini directly (independent of `_build_schema`'s own
    parsing) so a bug in derivation itself can't hide a genuine gap.
    """
    parser = ConfigParser()
    parser.optionxform = str
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")

    example_sections = set(parser.sections())
    wizard_sections = {s.section for s in config_wizard.WIZARD_SCHEMA}

    accounted_for = wizard_sections | config_wizard.EXCLUDED_SECTIONS
    missing = example_sections - accounted_for
    assert not missing, (
        f"example.config.ini has section(s) {sorted(missing)} that the wizard neither "
        "renders nor explicitly excludes -- add them to WIZARD_SCHEMA (automatic, just "
        "re-run) or to config_wizard.EXCLUDED_SECTIONS with a documented reason."
    )


def test_wizard_schema_covers_every_active_key_in_example_config():
    """Every uncommented key in example.config.ini's included sections is wizard-editable."""
    parser = ConfigParser()
    parser.optionxform = str
    parser.read(_EXAMPLE_CONFIG_PATH, encoding="utf-8")

    for section in parser.sections():
        if section in config_wizard.EXCLUDED_SECTIONS:
            continue
        spec = next(s for s in config_wizard.WIZARD_SCHEMA if s.section == section)
        known_keys = {f.key for f in spec.fields}
        assert known_keys == set(parser.options(section)), section


def test_excluded_sections_are_the_documented_three():
    assert frozenset({"paths", "openai", "github"}) == config_wizard.EXCLUDED_SECTIONS


def test_rag_gains_full_tuning_surface_not_just_original_six_fields():
    """The original #3354 schema only exposed 6 of [rag]'s ~45 keys (#3388's reported gap)."""
    spec = next(s for s in config_wizard.WIZARD_SCHEMA if s.section == "rag")
    keys = {f.key for f in spec.fields}
    for key in ("chunk_size", "chunk_overlap", "bm25_k1", "enable_reranking", "rerank_top_n"):
        assert key in keys


def test_previously_absent_sections_are_now_covered():
    covered = {s.section for s in config_wizard.WIZARD_SCHEMA}
    for section in (
        "cache",
        "web",
        "canary",
        "batch",
        "self_heal",
        "context",
        "prompt",
        "pdf",
    ):
        assert section in covered


# --- apply_updates: non-destructive, comment-preserving merge (#3388) ---

_SAMPLE_USER_CONFIG = """\
# My personal notes on this file
[nyxgpt]
# do not change this, it took forever to tune
default_model = my-favorite-model
chat_timeout_seconds = 999

[paths]
repo_dir = /Users/me/nyxGPT

[openai]
api_key = sk-hand-added-secret

[github]
pat = ghp_hand-added-token
agents_enabled = true

[mycustomsection]
custom_key = custom_value

[rag]
enable_chat_context = true
# a hand-added key that isn't part of any schema
my_extra_note = keep me
"""


def test_apply_updates_preserves_comments_and_key_order(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(_SAMPLE_USER_CONFIG)

    config_wizard.apply_updates(cfg_path, {"nyxgpt": {"chat_timeout_seconds": 120}})

    written = cfg_path.read_text()
    assert "# My personal notes on this file" in written
    assert "# do not change this, it took forever to tune" in written
    assert "chat_timeout_seconds = 120" in written
    # The untouched key stays exactly where it was, above the changed one.
    assert written.index("default_model") < written.index("chat_timeout_seconds")


def test_apply_updates_never_touches_excluded_sections(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(_SAMPLE_USER_CONFIG)

    config_wizard.apply_updates(cfg_path, {"nyxgpt": {"default_model": "new-model"}})

    written = cfg_path.read_text()
    parser = ConfigParser()
    parser.read_string(written)
    assert parser.get("paths", "repo_dir") == "/Users/me/nyxGPT"
    assert parser.get("openai", "api_key") == "sk-hand-added-secret"
    assert parser.get("github", "pat") == "ghp_hand-added-token"
    assert parser.getboolean("github", "agents_enabled") is True


def test_apply_updates_preserves_hand_added_section_and_keys(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(_SAMPLE_USER_CONFIG)

    config_wizard.apply_updates(cfg_path, {"rag": {"enable_chat_context": False}})

    written = cfg_path.read_text()
    parser = ConfigParser()
    parser.read_string(written)
    assert parser.get("mycustomsection", "custom_key") == "custom_value"
    assert parser.get("rag", "my_extra_note") == "keep me"
    assert parser.getboolean("rag", "enable_chat_context") is False


def test_apply_updates_appends_new_key_to_existing_section(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = keep-me\n")

    config_wizard.apply_updates(cfg_path, {"nyxgpt": {"chat_timeout_seconds": 42}})

    written = cfg_path.read_text()
    parser = ConfigParser()
    parser.read_string(written)
    assert parser.get("nyxgpt", "default_model") == "keep-me"
    assert parser.getint("nyxgpt", "chat_timeout_seconds") == 42


def test_apply_updates_full_regression_survives_intact(tmp_path):
    """Regression test required by #3388: excluded sections, hand-added keys, and comments
    all survive a wizard save untouched, only the touched fields change."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(_SAMPLE_USER_CONFIG)

    config_wizard.apply_updates(
        cfg_path,
        {"nyxgpt": {"default_model": "updated-model"}, "rag": {"enable_chat_context": False}},
    )

    written = cfg_path.read_text()
    assert "# My personal notes on this file" in written
    assert "# do not change this, it took forever to tune" in written
    assert "my_extra_note = keep me" in written

    parser = ConfigParser()
    parser.read_string(written)
    assert parser.get("nyxgpt", "default_model") == "updated-model"
    assert parser.getboolean("rag", "enable_chat_context") is False
    assert parser.get("paths", "repo_dir") == "/Users/me/nyxGPT"
    assert parser.get("openai", "api_key") == "sk-hand-added-secret"
    assert parser.get("github", "pat") == "ghp_hand-added-token"
    assert parser.get("mycustomsection", "custom_key") == "custom_value"


# --- find_stale_keys / remove_keys (drift reconciliation, #3388) ---


def test_find_stale_keys_reports_unknown_key_in_managed_section():
    cfg = _cfg(nyxgpt={"default_model": "a", "totally_made_up_key": "x"})
    stale = config_wizard.find_stale_keys(cfg)
    assert stale["nyxgpt"] == ["totally_made_up_key"]


def test_find_stale_keys_ignores_known_fields():
    cfg = _cfg(nyxgpt={"default_model": "a"})
    stale = config_wizard.find_stale_keys(cfg)
    assert "nyxgpt" not in stale


def test_find_stale_keys_ignores_excluded_sections():
    cfg = _cfg(paths={"repo_dir": "/x"}, openai={"api_key": "y"})
    stale = config_wizard.find_stale_keys(cfg)
    assert stale == {}


def test_find_stale_keys_ignores_sections_absent_entirely():
    cfg = _cfg()
    assert config_wizard.find_stale_keys(cfg) == {}


def test_remove_keys_deletes_only_the_named_stale_key(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[nyxgpt]\ndefault_model = keep-me\nretired_option = old\n\n[paths]\nrepo_dir = /x\n"
    )

    removed = config_wizard.remove_keys(cfg_path, {"nyxgpt": ["retired_option"]})

    assert removed == {"nyxgpt": ["retired_option"]}
    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("nyxgpt", "default_model") == "keep-me"
    assert not parser.has_option("nyxgpt", "retired_option")
    assert parser.get("paths", "repo_dir") == "/x"


def test_remove_keys_is_noop_for_key_that_does_not_exist(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = keep-me\n")

    removed = config_wizard.remove_keys(cfg_path, {"nyxgpt": ["does_not_exist"]})

    assert removed == {}
    assert cfg_path.read_text() == "[nyxgpt]\ndefault_model = keep-me\n"


def test_remove_keys_noop_when_file_missing(tmp_path):
    cfg_path = tmp_path / "config.ini"
    assert config_wizard.remove_keys(cfg_path, {"nyxgpt": ["x"]}) == {}
    assert not cfg_path.exists()


def test_apply_updates_never_deletes_anything():
    """`apply_updates` has no `remove` parameter at all -- a save can only add/update."""
    import inspect

    sig = inspect.signature(config_wizard.apply_updates)
    assert "remove" not in sig.parameters
