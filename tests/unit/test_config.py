from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest
from home_sandbox import REAL_HOME

from nyxgpt import cloud_secrets
from nyxgpt.config import (
    DEFAULT_CHAT_TIMEOUT_SECONDS,
    SECRETS_SYNC_MANIFEST,
    _expand_path,
    get_ann_oversample_factor,
    get_api_host,
    get_api_port,
    get_auth_api_key,
    get_auth_enabled,
    get_batch_enabled,
    get_batch_size,
    get_batch_wait_time_ms,
    get_canary_error_rate_threshold,
    get_canary_latency_p95_threshold_ms,
    get_canary_min_requests,
    get_canary_namespace,
    get_canary_step_percent,
    get_canary_total_replicas,
    get_cassandra_batch_query_concurrency,
    get_cassandra_batch_size,
    get_cassandra_health_check_interval,
    get_cassandra_pool_size,
    get_cassandra_reconnect_max_attempts,
    get_chat_timeout_seconds,
    get_context_warning_threshold,
    get_context_window_size,
    get_effective_config_summary,
    get_error_tracking_config,
    get_error_tracking_enabled,
    get_github_pat,
    get_github_repo_name,
    get_github_repo_owner,
    get_log_aggregation_config,
    get_log_aggregation_enabled,
    get_monitoring_config,
    get_monitoring_enabled,
    get_monitoring_grafana_admin_password,
    get_monitoring_slack_bot_token,
    get_monitoring_slack_webhook_url,
    get_openai_api_key,
    get_prompt_mode_enabled,
    get_prompt_mode_long_threshold,
    get_prompt_mode_short_threshold,
    get_rag_chat_context_max_chars,
    get_rag_chat_top_k,
    get_rag_context_format,
    get_rag_debug_mode,
    get_rag_dedupe,
    get_rag_enabled,
    get_rag_good_score_threshold,
    get_rag_include_headers,
    get_rag_include_scores,
    get_rag_instruction_template,
    get_rag_max_chunks,
    get_rag_medium_score_threshold,
    get_rag_min_score,
    get_rate_limit_config,
    get_rate_limit_enabled,
    get_secrets_provider,
    get_secrets_region,
    get_secrets_secretsmanager_id,
    get_secrets_ssm_prefix,
    get_self_heal_backoff_seconds,
    get_self_heal_check_interval_seconds,
    get_self_heal_default_enabled,
    get_self_heal_max_consecutive_restarts,
    get_system_prompt_minimize,
    get_tracing_config,
    get_tracing_enabled,
    get_vector_similarity_function,
    get_vectorstore_dir,
    is_loopback_host,
    load_config,
    log_effective_config,
    redact_url_userinfo,
    reset_fallback_warnings,
    validate_bind_security,
    validate_config,
)

pytestmark = pytest.mark.unit


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_load_config_defaults_exist() -> None:
    cfg = load_config(None)

    assert cfg.get("ollama", "base_url", fallback=None)
    assert cfg.get("nyxgpt", "default_model", fallback=None)


def test_load_config_from_explicit_path(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert cfg.get("nyxgpt", "default_model") == "llama3.1:8b"
    assert cfg.get("ollama", "base_url") == "http://127.0.0.1:11434"


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.ini"
    with pytest.raises(FileNotFoundError):
        load_config(str(missing))


def test_load_config_expands_tilde_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a home dir and a config file under it
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))

    ini = fake_home / ".nyxGPT" / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b
""".lstrip(),
    )

    cfg = load_config("~/.nyxGPT/config.ini")
    assert cfg.get("nyxgpt", "default_model") == "llama3.1:8b"


def test_default_log_dir_is_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # This test encodes the desired default: ~/.nyxGPT/logs
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))

    cfg = load_config(None)
    # We will implement nyxgpt.log_dir; until then, this uses fallback.
    log_dir = cfg.get("nyxgpt", "log_dir", fallback=str(Path("~/.nyxGPT/logs").expanduser()))

    # Must resolve under fake HOME
    resolved = Path(log_dir).expanduser()
    assert str(resolved).startswith(str(fake_home))


def test_config_allows_overriding_log_dir(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        f"""
[nyxgpt]
log_dir = {tmp_path / "logs"}
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert cfg.get("nyxgpt", "log_dir") == str(tmp_path / "logs")


def test_chat_timeout_default_can_be_read(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
chat_timeout_seconds = 180
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert cfg.getint("nyxgpt", "chat_timeout_seconds") == 180


def test_load_config_missing_file_raises_error() -> None:
    """load_config should raise FileNotFoundError for missing config file."""
    with pytest.raises(FileNotFoundError, match=r"Missing config file.*config\.ini"):
        load_config("/nonexistent/path/config.ini")


def test_load_config_prints_validation_errors_on_first_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """load_config prints validation errors to stderr on first load, but doesn't raise."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b
""".lstrip(),
    )

    # Force the "first load" path regardless of test ordering/module state.
    monkeypatch.setattr("nyxgpt.config._CACHED_CFG", None)
    monkeypatch.setattr("nyxgpt.config._CACHED_PATH", None)
    monkeypatch.setattr("nyxgpt.config._CACHED_MTIME_NS", None)

    cfg = load_config(str(ini))

    captured = capsys.readouterr()
    assert "ERROR: Configuration validation failed" in captured.err
    assert "Missing required section: [ollama]" in captured.err
    assert cfg.get("nyxgpt", "default_model") == "llama3.1:8b"


def test_validate_config_detects_invalid_port(tmp_path: Path) -> None:
    """validate_config should detect invalid port values."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[api]
port = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("port" in err.lower() for err in errors)


def test_validate_config_detects_negative_port(tmp_path: Path) -> None:
    """validate_config should detect negative port values."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[api]
port = -1
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("port" in err.lower() and "1024-65535" in err for err in errors)


def test_validate_config_detects_port_too_large(tmp_path: Path) -> None:
    """validate_config should detect port values > 65535."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[api]
port = 99999
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("port" in err.lower() and "1024-65535" in err for err in errors)


def test_get_api_port_handles_invalid_type_gracefully(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """get_api_port should return default value for invalid port types."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[api]
port = invalid
""".lstrip(),
    )

    cfg = load_config(str(ini))
    # Should return default port (8000) and log a warning
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    port = get_api_port(cfg)

    assert port == 8000
    assert "Invalid api.port" in caplog.text


def test_get_prompt_mode_enabled_default(tmp_path: Path) -> None:
    """get_prompt_mode_enabled should return False by default."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_enabled(cfg) is False


def test_get_prompt_mode_enabled_true(tmp_path: Path) -> None:
    """get_prompt_mode_enabled should return configured value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
adaptive_mode_enabled = true
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_enabled(cfg) is True


def test_get_prompt_mode_enabled_false(tmp_path: Path) -> None:
    """get_prompt_mode_enabled should return configured value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
adaptive_mode_enabled = false
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_enabled(cfg) is False


def test_get_prompt_mode_enabled_invalid_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """get_prompt_mode_enabled should handle invalid values gracefully."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
adaptive_mode_enabled = not_a_boolean
""".lstrip(),
    )

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    enabled = get_prompt_mode_enabled(cfg)

    assert enabled is False
    assert "Invalid prompt.adaptive_mode_enabled" in caplog.text


def test_get_prompt_mode_short_threshold_default(tmp_path: Path) -> None:
    """get_prompt_mode_short_threshold should return default value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_short_threshold(cfg) == 3


def test_get_prompt_mode_short_threshold_configured(tmp_path: Path) -> None:
    """get_prompt_mode_short_threshold should return configured value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
short_threshold = 5
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_short_threshold(cfg) == 5


def test_get_prompt_mode_short_threshold_minimum_value(tmp_path: Path) -> None:
    """get_prompt_mode_short_threshold should enforce minimum value of 1."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
short_threshold = 0
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_short_threshold(cfg) == 1


def test_get_prompt_mode_short_threshold_invalid_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """get_prompt_mode_short_threshold should handle invalid values gracefully."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
short_threshold = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    threshold = get_prompt_mode_short_threshold(cfg)

    assert threshold == 3
    assert "Invalid prompt.short_threshold" in caplog.text


def test_get_prompt_mode_long_threshold_default(tmp_path: Path) -> None:
    """get_prompt_mode_long_threshold should return default value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_long_threshold(cfg) == 10


def test_get_prompt_mode_long_threshold_configured(tmp_path: Path) -> None:
    """get_prompt_mode_long_threshold should return configured value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
short_threshold = 3
long_threshold = 15
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_prompt_mode_long_threshold(cfg) == 15


def test_get_prompt_mode_long_threshold_enforces_minimum(tmp_path: Path) -> None:
    """get_prompt_mode_long_threshold should be greater than short_threshold."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
short_threshold = 10
long_threshold = 5
""".lstrip(),
    )

    cfg = load_config(str(ini))
    # long_threshold should be at least short_threshold + 1
    assert get_prompt_mode_long_threshold(cfg) == 11


def test_get_prompt_mode_long_threshold_invalid_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """get_prompt_mode_long_threshold should handle invalid values gracefully."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[prompt]
long_threshold = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    threshold = get_prompt_mode_long_threshold(cfg)

    assert threshold == 10
    assert "Invalid prompt.long_threshold" in caplog.text


def test_get_rag_good_score_threshold_default(tmp_path: Path) -> None:
    """get_rag_good_score_threshold should return default value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_good_score_threshold(cfg) == 0.7


def test_get_rag_good_score_threshold_configured(tmp_path: Path) -> None:
    """get_rag_good_score_threshold should return configured value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[rag]
good_score_threshold = 0.8
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_good_score_threshold(cfg) == 0.8


def test_get_rag_medium_score_threshold_default(tmp_path: Path) -> None:
    """get_rag_medium_score_threshold should return default value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_medium_score_threshold(cfg) == 0.4


def test_get_rag_medium_score_threshold_configured(tmp_path: Path) -> None:
    """get_rag_medium_score_threshold should return configured value."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[rag]
medium_score_threshold = 0.5
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_medium_score_threshold(cfg) == 0.5


def test_get_rag_enabled_default_is_false(tmp_path: Path) -> None:
    """With neither key set, RAG defaults to disabled."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_enabled(cfg) is False


def test_get_rag_enabled_reads_canonical_key(tmp_path: Path) -> None:
    """`[rag] enable_chat_context` is the canonical RAG on/off switch.

    This is the key the chat/session runtime reads (nyxgpt/chat.py,
    nyxgpt/sessions.py), so get_rag_enabled() must agree with it -- this is
    what the admin health/overview/config endpoints call. See #3183.
    """
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[rag]
enable_chat_context = true
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_enabled(cfg) is True


def test_get_rag_enabled_falls_back_to_legacy_alias(tmp_path: Path) -> None:
    """The deprecated `[rag] enabled` alias is honored when the canonical
    `enable_chat_context` key is not explicitly set."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[rag]
enabled = true
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_enabled(cfg) is True


def test_get_rag_enabled_canonical_key_takes_precedence(tmp_path: Path) -> None:
    """When both keys are set, the canonical `enable_chat_context` wins."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[rag]
enable_chat_context = false
enabled = true
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_rag_enabled(cfg) is False


def test_validate_config_detects_negative_context_window(tmp_path: Path) -> None:
    """validate_config should detect negative context window size."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
default_window_size = -100
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("context.default_window_size" in err and "at least 100" in err for err in errors)


def test_validate_config_detects_zero_context_window(tmp_path: Path) -> None:
    """validate_config should detect zero context window size."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
default_window_size = 0
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("context.default_window_size" in err and "at least 100" in err for err in errors)


def test_validate_config_detects_too_small_context_window(tmp_path: Path) -> None:
    """validate_config should detect context window size below minimum."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
default_window_size = 50
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("context.default_window_size" in err and "at least 100" in err for err in errors)


def test_validate_config_detects_too_large_context_window(tmp_path: Path) -> None:
    """validate_config should detect context window size above maximum."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
default_window_size = 2000000
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "context.default_window_size" in err and "not exceed 1,000,000" in err for err in errors
    )


def test_validate_config_detects_invalid_context_window_type(tmp_path: Path) -> None:
    """validate_config should detect non-integer context window size."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
default_window_size = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "context.default_window_size" in err and "must be an integer" in err for err in errors
    )


def test_validate_config_accepts_valid_context_window(tmp_path: Path) -> None:
    """validate_config should accept valid context window sizes."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
default_window_size = 8192
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    # Should have no errors related to context.default_window_size
    assert not any("context.default_window_size" in err for err in errors)


def test_validate_config_detects_invalid_warning_threshold_negative(
    tmp_path: Path,
) -> None:
    """validate_config should detect negative warning threshold."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
warning_threshold = -0.5
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "context.warning_threshold" in err and "between 0.0 and 1.0" in err for err in errors
    )


def test_validate_config_detects_invalid_warning_threshold_too_large(
    tmp_path: Path,
) -> None:
    """validate_config should detect warning threshold > 1.0."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
warning_threshold = 1.5
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "context.warning_threshold" in err and "between 0.0 and 1.0" in err for err in errors
    )


def test_validate_config_detects_invalid_warning_threshold_type(tmp_path: Path) -> None:
    """validate_config should detect non-float warning threshold."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
warning_threshold = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("context.warning_threshold" in err and "must be a float" in err for err in errors)


def test_validate_config_accepts_valid_warning_threshold(tmp_path: Path) -> None:
    """validate_config should accept valid warning threshold."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
warning_threshold = 0.8
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    # Should have no errors related to context.warning_threshold
    assert not any("context.warning_threshold" in err for err in errors)


def test_validate_config_accepts_boundary_warning_thresholds(tmp_path: Path) -> None:
    """validate_config should accept warning threshold at boundaries (0.0, 1.0)."""
    # Test 0.0
    ini = tmp_path / "config1.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
warning_threshold = 0.0
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)
    assert not any("context.warning_threshold" in err for err in errors)

    # Test 1.0
    ini2 = tmp_path / "config2.ini"
    _write(
        ini2,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
warning_threshold = 1.0
""".lstrip(),
    )

    cfg2 = load_config(str(ini2))
    errors2 = validate_config(cfg2)
    assert not any("context.warning_threshold" in err for err in errors2)


def test_validate_config_detects_invalid_model_specific_override(tmp_path: Path) -> None:
    """validate_config should detect invalid model-specific context window override."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
context_window_llama3_1_8b = -1000
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "context.context_window_llama3_1_8b" in err and "at least 100" in err for err in errors
    )


def test_validate_config_detects_too_large_model_specific_override(
    tmp_path: Path,
) -> None:
    """validate_config should detect model-specific override above maximum."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
context_window_llama3_1_8b = 5000000
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "context.context_window_llama3_1_8b" in err and "not exceed 1,000,000" in err
        for err in errors
    )


def test_validate_config_detects_invalid_model_specific_override_type(
    tmp_path: Path,
) -> None:
    """validate_config should detect non-integer model-specific override."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
context_window_mistral = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "context.context_window_mistral" in err and "must be an integer" in err for err in errors
    )


def test_validate_config_accepts_valid_model_specific_override(tmp_path: Path) -> None:
    """validate_config should accept valid model-specific overrides."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[context]
default_window_size = 8192
context_window_llama3_1_8b = 131072
context_window_mistral = 8192
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    # Should have no errors related to context settings
    assert not any("context." in err for err in errors)


def test_get_monitoring_grafana_admin_password_reads_value(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[monitoring]
enabled = true
grafana_admin_password = super-secret-value
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_monitoring_grafana_admin_password(cfg) == "super-secret-value"


def test_get_monitoring_grafana_admin_password_defaults_to_empty(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_monitoring_grafana_admin_password(cfg) == ""


def test_get_monitoring_slack_webhook_url_reads_value(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[monitoring]
enabled = true
slack_webhook_url = https://hooks.slack.com/services/T00/B00/XXX
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_monitoring_slack_webhook_url(cfg) == "https://hooks.slack.com/services/T00/B00/XXX"


def test_get_monitoring_slack_webhook_url_defaults_to_empty(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_monitoring_slack_webhook_url(cfg) == ""


def test_get_monitoring_config_never_exposes_grafana_admin_password(tmp_path: Path) -> None:
    """get_monitoring_config() is returned verbatim by GET /api/v1/monitoring --
    the Grafana admin password must never appear in it (see #3194)."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434

[monitoring]
enabled = true
grafana_admin_password = super-secret-value
""".lstrip(),
    )

    cfg = load_config(str(ini))
    monitoring_config = get_monitoring_config(cfg)

    assert "grafana_admin_password" not in monitoring_config
    assert "super-secret-value" not in monitoring_config.values()


# ---------------------------------------------------------------------------
# validate_config: additional branches (api.host, ollama.base_url, rag ints,
# chunk_overlap/chunk_size relationship)
# ---------------------------------------------------------------------------


def test_validate_config_detects_blank_api_host(tmp_path: Path) -> None:
    """validate_config should detect a blank/whitespace-only api.host."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[api]
host =
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("Invalid api.host" in err and "cannot be empty" in err for err in errors)


def test_validate_config_detects_invalid_ollama_base_url(tmp_path: Path) -> None:
    """validate_config should detect an ollama.base_url without http(s):// scheme."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[ollama]
base_url = ftp://example.com
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("Invalid ollama.base_url" in err and "must start with http" in err for err in errors)


def test_validate_config_detects_rag_int_setting_out_of_range(tmp_path: Path) -> None:
    """validate_config should detect a parseable but out-of-range rag int setting."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[rag]
chat_top_k = 500
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("Invalid rag.chat_top_k" in err and "must be 1-100" in err for err in errors)


def test_validate_config_detects_rag_int_setting_non_integer(tmp_path: Path) -> None:
    """validate_config should detect a non-integer rag int setting."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[rag]
chat_top_k = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any("Invalid rag.chat_top_k" in err and "must be an integer" in err for err in errors)


def test_validate_config_detects_chunk_overlap_gte_chunk_size(tmp_path: Path) -> None:
    """validate_config should detect chunk_overlap >= chunk_size."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[rag]
chunk_size = 500
chunk_overlap = 500
""".lstrip(),
    )

    cfg = load_config(str(ini))
    errors = validate_config(cfg)

    assert len(errors) > 0
    assert any(
        "Invalid RAG config: chunk_overlap" in err and "must be less than chunk_size" in err
        for err in errors
    )


# ---------------------------------------------------------------------------
# load_config: stat() failure branch (falls back to mtime_ns = None)
# ---------------------------------------------------------------------------


def test_load_config_stat_failure_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config should tolerate config_path.stat() raising and still load the file."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
default_model = llama3.1:8b
""".lstrip(),
    )

    original_stat = Path.stat
    call_count = {"n": 0}

    def bad_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self == ini:
            # The first call comes from config_path.exists() (line 165), which
            # must succeed so we reach the explicit stat() call on line 172 --
            # that second call is the one we want to fail.
            call_count["n"] += 1
            if call_count["n"] > 1:
                raise OSError("simulated stat failure")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", bad_stat)

    cfg = load_config(str(ini))
    assert cfg.get("nyxgpt", "default_model") == "llama3.1:8b"


# ---------------------------------------------------------------------------
# get_chat_timeout_seconds: except branch
# ---------------------------------------------------------------------------


def test_get_chat_timeout_seconds_invalid_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[nyxgpt]
chat_timeout_seconds = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    timeout = get_chat_timeout_seconds(cfg)

    # Pins agreement, not a literal. This asserted 180 while the fallback said
    # 300, so it certified the drift instead of catching it: an operator with a
    # malformed value got the very regression the raise exists to remove, under
    # a log line that said otherwise (#4028 review).
    assert timeout == DEFAULT_CHAT_TIMEOUT_SECONDS
    assert "Invalid nyxgpt.chat_timeout_seconds" in caplog.text
    assert str(DEFAULT_CHAT_TIMEOUT_SECONDS) in caplog.text, (
        "the warning must name the value actually returned -- a log that says "
        "one number while the code returns another misdirects the debugging"
    )


def test_the_valid_and_invalid_paths_return_the_same_shipped_default(tmp_path: Path) -> None:
    """An absent value and a malformed one must not disagree about the default."""
    absent = tmp_path / "absent.ini"
    _write(absent, "[nyxgpt]\n")
    broken = tmp_path / "broken.ini"
    _write(broken, "[nyxgpt]\nchat_timeout_seconds = abc\n")

    assert get_chat_timeout_seconds(load_config(str(absent))) == DEFAULT_CHAT_TIMEOUT_SECONDS
    assert get_chat_timeout_seconds(load_config(str(broken))) == DEFAULT_CHAT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# get_vectorstore_dir / get_api_host: never directly exercised
# ---------------------------------------------------------------------------


def test_expand_path_refuses_exact_allowed_roots(monkeypatch) -> None:
    """A configured path resolving EXACTLY to $HOME or the temp root -- rather
    than strictly inside one -- is refused: the barrier accepts descendants
    only (`startswith(root + os.sep)`). Pins the intentional behavior change
    from the single-condition guard restructure (PR #3657).

    `$HOME` is pointed back at the machine's real home for the home half.
    Since #4020 the suite's own `$HOME` is a temp directory, so a path equal to
    it is a strict *descendant* of the temp root and the second branch accepts
    it -- which would make this assertion pass or fail for a reason that has
    nothing to do with the guard it is about. The two roots have to be
    genuinely disjoint for "exactly a root is refused" to mean anything.
    """
    monkeypatch.setenv("HOME", str(REAL_HOME))
    with pytest.raises(ValueError, match="allowed data area"):
        _expand_path(str(REAL_HOME))
    with pytest.raises(ValueError, match="allowed data area"):
        _expand_path(tempfile.gettempdir())


def test_expand_path_accepts_descendants_of_allowed_roots(tmp_path: Path) -> None:
    """Sanity companion to the exact-root refusal: strict descendants of both
    allowed roots still resolve."""
    assert _expand_path(str(tmp_path / "data")) == (tmp_path / "data").resolve()
    home_child = Path.home() / ".nyxGPT" / "sessions"
    assert _expand_path(str(home_child)) == home_child.resolve()


def test_get_vectorstore_dir_default(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")

    cfg = load_config(str(ini))
    assert get_vectorstore_dir(cfg) == Path.home() / ".nyxGPT" / "vectorstore"


def test_get_vectorstore_dir_override(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    custom = tmp_path / "custom-vectorstore"
    _write(
        ini,
        f"""
[nyxgpt]
vectorstore_dir = {custom}
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_vectorstore_dir(cfg) == custom


def test_get_api_host_default(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")

    cfg = load_config(str(ini))
    assert get_api_host(cfg) == "127.0.0.1"


def test_get_api_host_override(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[api]
host = 0.0.0.0
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_api_host(cfg) == "0.0.0.0"


# ---------------------------------------------------------------------------
# is_loopback_host / get_auth_enabled / validate_bind_security
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("  localhost  ", True),
        ("::1", True),
        ("127.5.6.7", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("192.168.1.1", False),
        ("::", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert is_loopback_host(host) is expected


def test_get_auth_enabled_default_false(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")

    cfg = load_config(str(ini))
    assert get_auth_enabled(cfg) is False


def test_get_auth_enabled_true(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[auth]\nenabled = true\n")

    cfg = load_config(str(ini))
    assert get_auth_enabled(cfg) is True


def test_get_auth_enabled_invalid_falls_back_to_false(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[auth]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    assert get_auth_enabled(cfg) is False
    assert "Invalid auth.enabled" in caplog.text


@pytest.mark.parametrize(
    "host,auth_enabled,should_refuse",
    [
        ("127.0.0.1", False, False),
        ("127.0.0.1", True, False),
        ("localhost", False, False),
        ("localhost", True, False),
        ("::1", False, False),
        ("0.0.0.0", False, True),
        ("0.0.0.0", True, False),
        ("10.0.0.5", False, True),
        ("10.0.0.5", True, False),
        ("192.168.1.100", False, True),
        ("192.168.1.100", True, False),
    ],
)
def test_validate_bind_security_refusal_matrix(
    tmp_path: Path, host: str, auth_enabled: bool, should_refuse: bool
) -> None:
    """The host x auth refusal matrix: non-loopback + auth-off is the only refused cell."""
    ini = tmp_path / "config.ini"
    _write(
        ini,
        f"[api]\nhost = {host}\n[auth]\nenabled = {'true' if auth_enabled else 'false'}\n",
    )

    cfg = load_config(str(ini))
    error = validate_bind_security(cfg)
    if should_refuse:
        assert error is not None
        assert host in error
        assert "nyxgpt wizard" in error
    else:
        assert error is None


# ---------------------------------------------------------------------------
# get_canary_namespace
# ---------------------------------------------------------------------------


def test_get_canary_namespace_default() -> None:
    cfg = load_config(None)
    assert get_canary_namespace(cfg) == "nyxgpt"


def test_get_canary_namespace_override(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[canary]\nnamespace = canary-ns\n")

    cfg = load_config(str(ini))
    assert get_canary_namespace(cfg) == "canary-ns"


# ---------------------------------------------------------------------------
# Canary getters: except branches
# ---------------------------------------------------------------------------


def test_get_canary_total_replicas_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[canary]\ntotal_replicas = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_canary_total_replicas(cfg) == 4


def test_get_canary_step_percent_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[canary]\nstep_percent = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_canary_step_percent(cfg) == 25


def test_get_canary_error_rate_threshold_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[canary]\nerror_rate_threshold_percent = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_canary_error_rate_threshold(cfg) == 5.0


def test_get_canary_latency_p95_threshold_ms_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[canary]\nlatency_p95_threshold_ms = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_canary_latency_p95_threshold_ms(cfg) == 2000.0


def test_get_canary_min_requests_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[canary]\nmin_requests_for_evaluation = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_canary_min_requests(cfg) == 20


# ---------------------------------------------------------------------------
# get_rag_enabled / _get_rag_enabled_legacy_alias: except branches
# ---------------------------------------------------------------------------


def test_get_rag_enabled_invalid_canonical_falls_through_to_legacy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid enable_chat_context should log and fall through to the legacy alias."""
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nenable_chat_context = not_a_boolean\n")

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    result = get_rag_enabled(cfg)

    assert result is False
    assert "Invalid rag.enable_chat_context" in caplog.text


def test_get_rag_enabled_legacy_alias_invalid_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid [rag] enabled (legacy alias) should log and return False."""
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    result = get_rag_enabled(cfg)

    assert result is False
    assert "Invalid rag.enabled" in caplog.text


# ---------------------------------------------------------------------------
# Simple rag getters: except branches (all fall back to their documented default)
# ---------------------------------------------------------------------------


def test_get_rag_chat_top_k_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nchat_top_k = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_rag_chat_top_k(cfg) == 3


def test_get_rag_min_score_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nmin_score = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_rag_min_score(cfg) == 0.0


def test_get_rag_max_chunks_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nmax_chunks = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_rag_max_chunks(cfg) == 6


def test_get_rag_chat_context_max_chars_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nchat_context_max_chars = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_rag_chat_context_max_chars(cfg) == 2400


def test_get_rag_dedupe_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ndedupe = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_rag_dedupe(cfg) is True


def test_get_rag_include_scores_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ninclude_scores = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_rag_include_scores(cfg) is False


def test_get_rag_include_headers_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ninclude_headers = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_rag_include_headers(cfg) is True


def test_get_rag_debug_mode_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ndebug_mode = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_rag_debug_mode(cfg) is False


def test_get_rag_good_score_threshold_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ngood_score_threshold = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_rag_good_score_threshold(cfg) == 0.7


def test_get_rag_medium_score_threshold_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nmedium_score_threshold = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_rag_medium_score_threshold(cfg) == 0.4


# ---------------------------------------------------------------------------
# Rate limit getters: except branches
# ---------------------------------------------------------------------------


def test_get_rate_limit_enabled_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rate_limit]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_rate_limit_enabled(cfg) is False


def test_get_rate_limit_config_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rate_limit]\nrequests_per_second = not_a_number\n")

    cfg = load_config(str(ini))
    result = get_rate_limit_config(cfg)

    assert result == {"requests_per_second": 10, "burst_size": 20}


# ---------------------------------------------------------------------------
# get_context_window_size: model-specific inner except and outer except
# ---------------------------------------------------------------------------


def test_get_context_window_size_model_specific_invalid_falls_back_to_default(
    tmp_path: Path,
) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[context]
default_window_size = 4096
context_window_mistral = not_a_number
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert get_context_window_size(cfg, model="mistral") == 4096


def test_get_context_window_size_default_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[context]\ndefault_window_size = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_context_window_size(cfg) == 8192


def test_get_context_warning_threshold_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[context]\nwarning_threshold = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_context_warning_threshold(cfg) == 0.8


def test_get_context_warning_threshold_valid_value_is_clamped(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[context]\nwarning_threshold = 1.5\n")

    cfg = load_config(str(ini))
    assert get_context_warning_threshold(cfg) == 1.0


def test_get_system_prompt_minimize_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\nsystem_prompt_minimize = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_system_prompt_minimize(cfg) is False


# ---------------------------------------------------------------------------
# get_rag_instruction_template / get_rag_context_format / get_vector_similarity_function:
# except branches, triggered via a stray "%" causing configparser's string
# interpolation to raise InterpolationSyntaxError at .get() time.
# ---------------------------------------------------------------------------


def test_get_rag_instruction_template_interpolation_error_falls_back_to_default(
    tmp_path: Path,
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ninstruction_template = 50% off today\n")

    cfg = load_config(str(ini))
    result = get_rag_instruction_template(cfg)

    assert "Use the retrieved context below" in result


def test_get_rag_context_format_interpolation_error_falls_back_to_default(
    tmp_path: Path,
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncontext_format = 50% off today\n")

    cfg = load_config(str(ini))
    result = get_rag_context_format(cfg)

    assert "BEGIN RETRIEVED CONTEXT" in result


def test_get_vector_similarity_function_interpolation_error_falls_back_to_cosine(
    tmp_path: Path,
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nvector_similarity_function = cos%ine\n")

    cfg = load_config(str(ini))
    assert get_vector_similarity_function(cfg) == "cosine"


# ---------------------------------------------------------------------------
# Cassandra getters: except branches
# ---------------------------------------------------------------------------


def test_get_cassandra_pool_size_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_pool_size = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_cassandra_pool_size(cfg) == 2


def test_get_cassandra_pool_size_valid_value_is_clamped(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_pool_size = 8\n")

    cfg = load_config(str(ini))
    assert get_cassandra_pool_size(cfg) == 8


def test_get_cassandra_health_check_interval_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_health_check_interval = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_cassandra_health_check_interval(cfg) == 30.0


def test_get_cassandra_health_check_interval_valid_value_is_clamped(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_health_check_interval = 60.0\n")

    cfg = load_config(str(ini))
    assert get_cassandra_health_check_interval(cfg) == 60.0


def test_get_cassandra_reconnect_max_attempts_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_reconnect_max_attempts = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_cassandra_reconnect_max_attempts(cfg) == 3


def test_get_cassandra_reconnect_max_attempts_valid_value_is_clamped(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_reconnect_max_attempts = 5\n")

    cfg = load_config(str(ini))
    assert get_cassandra_reconnect_max_attempts(cfg) == 5


def test_get_cassandra_batch_size_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_batch_size = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_cassandra_batch_size(cfg) == 20


def test_get_cassandra_batch_size_valid_value_is_clamped(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_batch_size = 50\n")

    cfg = load_config(str(ini))
    assert get_cassandra_batch_size(cfg) == 50


def test_get_ann_oversample_factor_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nann_oversample_factor = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_ann_oversample_factor(cfg) == 1.0


def test_get_cassandra_batch_query_concurrency_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\ncassandra_batch_query_concurrency = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_cassandra_batch_query_concurrency(cfg) == 4


# ---------------------------------------------------------------------------
# Batch getters: except branches
# ---------------------------------------------------------------------------


def test_get_batch_enabled_invalid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[batch]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    assert get_batch_enabled(cfg) is False
    assert "Invalid batch.enabled" in caplog.text


def test_get_batch_size_valid_value_is_clamped(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[batch]\nbatch_size = 8\n")

    cfg = load_config(str(ini))
    assert get_batch_size(cfg) == 8


def test_get_batch_size_invalid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[batch]\nbatch_size = not_a_number\n")

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    assert get_batch_size(cfg) == 4
    assert "Invalid batch.batch_size" in caplog.text


def test_get_batch_wait_time_ms_valid_value_is_clamped(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[batch]\nwait_time_ms = 250\n")

    cfg = load_config(str(ini))
    assert get_batch_wait_time_ms(cfg) == 250


def test_get_batch_wait_time_ms_invalid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[batch]\nwait_time_ms = not_a_number\n")

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    assert get_batch_wait_time_ms(cfg) == 100
    assert "Invalid batch.wait_time_ms" in caplog.text


# ---------------------------------------------------------------------------
# Observability getters: except branches
# ---------------------------------------------------------------------------


def test_get_tracing_enabled_defaults_to_true(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")

    cfg = load_config(str(ini))
    assert get_tracing_enabled(cfg) is True


def test_get_tracing_enabled_invalid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[tracing]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    caplog.set_level(logging.WARNING, logger="nyxgpt.config")
    assert get_tracing_enabled(cfg) is True
    assert "Invalid tracing.enabled" in caplog.text


def test_get_tracing_config_returns_expected_shape(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[tracing]
enabled = true
service_name = my-service
otlp_endpoint = http://collector:4318/v1/traces
jaeger_ui_url = http://jaeger:16686
""".lstrip(),
    )

    cfg = load_config(str(ini))
    result = get_tracing_config(cfg)

    assert result == {
        "enabled": True,
        "service_name": "my-service",
        "otlp_endpoint": "http://collector:4318/v1/traces",
        "jaeger_ui_url": "http://jaeger:16686",
    }


def test_get_error_tracking_enabled_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[error_tracking]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_error_tracking_enabled(cfg) is False


def test_get_error_tracking_config_invalid_traces_sample_rate(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[error_tracking]\ntraces_sample_rate = not_a_number\n")

    cfg = load_config(str(ini))
    result = get_error_tracking_config(cfg)

    assert result["traces_sample_rate"] == 0.0


def test_get_monitoring_enabled_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[monitoring]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_monitoring_enabled(cfg) is False


def test_get_log_aggregation_enabled_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[log_aggregation]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_log_aggregation_enabled(cfg) is False


def test_get_log_aggregation_config_returns_expected_shape(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[log_aggregation]
enabled = true
grafana_explore_url = http://grafana:3001/explore
""".lstrip(),
    )

    cfg = load_config(str(ini))
    result = get_log_aggregation_config(cfg)

    assert result == {
        "enabled": True,
        "grafana_explore_url": "http://grafana:3001/explore",
    }


# ---------------------------------------------------------------------------
# Self-heal getters: except branches
# ---------------------------------------------------------------------------


def test_get_self_heal_default_enabled_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[self_heal]\nenabled = not_a_boolean\n")

    cfg = load_config(str(ini))
    assert get_self_heal_default_enabled(cfg) is False


def test_get_self_heal_check_interval_seconds_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[self_heal]\ncheck_interval_seconds = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_self_heal_check_interval_seconds(cfg) == 15.0


def test_get_self_heal_max_consecutive_restarts_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[self_heal]\nmax_consecutive_restarts = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_self_heal_max_consecutive_restarts(cfg) == 5


def test_get_self_heal_backoff_seconds_invalid(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[self_heal]\nbackoff_seconds = not_a_number\n")

    cfg = load_config(str(ini))
    assert get_self_heal_backoff_seconds(cfg) == 30.0


# ---------------------------------------------------------------------------
# _log_fallback_once / effective config summary (#3415 gap 4)
# ---------------------------------------------------------------------------


def test_fallback_warning_logs_once_per_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nchat_top_k = not_a_number\n")
    cfg = load_config(str(ini))

    with caplog.at_level(logging.WARNING, logger="nyxgpt.config"):
        assert get_rag_chat_top_k(cfg) == 3
        assert get_rag_chat_top_k(cfg) == 3  # second call, same key

    matching = [r for r in caplog.records if "rag.chat_top_k" in r.getMessage()]
    assert len(matching) == 1, "Expected the fallback warning to log only once per key"


def test_fallback_warning_dedup_reset_allows_relogging(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[rag]\nmin_score = not_a_number\n")
    cfg = load_config(str(ini))

    with caplog.at_level(logging.WARNING, logger="nyxgpt.config"):
        get_rag_min_score(cfg)
        reset_fallback_warnings()
        get_rag_min_score(cfg)

    matching = [r for r in caplog.records if "rag.min_score" in r.getMessage()]
    assert len(matching) == 2


def test_get_effective_config_summary_redacts_secrets(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        "[error_tracking]\ndsn = https://secret@example.com/1\n"
        "[monitoring]\ngrafana_admin_password = hunter2\n"
        "slack_webhook_url = https://hooks.slack.com/services/T00/B00/XXX\n",
    )
    cfg = load_config(str(ini))

    summary = get_effective_config_summary(cfg)

    assert summary["error_tracking.dsn"] == "***redacted***"
    assert summary["monitoring.grafana_admin_password"] == "***redacted***"
    assert summary["monitoring.slack_webhook_url"] == "***redacted***"
    assert "hunter2" not in str(summary)
    assert "secret@example.com" not in str(summary)
    assert "hooks.slack.com" not in str(summary)
    assert summary["tracing.enabled"] is True


def test_get_effective_config_summary_empty_secrets_stay_empty(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))

    summary = get_effective_config_summary(cfg)

    assert summary["error_tracking.dsn"] == ""
    assert summary["monitoring.grafana_admin_password"] == ""
    assert summary["monitoring.slack_webhook_url"] == ""


# --- Guided secrets getters (#3505) ---


def test_get_monitoring_slack_bot_token_reads_value(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[monitoring]\nslack_bot_token = xoxb-1234-5678\n")
    cfg = load_config(str(ini))
    assert get_monitoring_slack_bot_token(cfg) == "xoxb-1234-5678"


def test_get_monitoring_slack_bot_token_defaults_to_empty(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))
    assert get_monitoring_slack_bot_token(cfg) == ""


def test_get_openai_api_key_reads_value(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[openai]\napi_key = sk-abc123\n")
    cfg = load_config(str(ini))
    assert get_openai_api_key(cfg) == "sk-abc123"


def test_get_github_pat_reads_value(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[github]\npat = ghp_abc123\n")
    cfg = load_config(str(ini))
    assert get_github_pat(cfg) == "ghp_abc123"


# --- Cloud secrets (SSM / Secrets Manager, P6-10, #3507) ---


@pytest.fixture(autouse=True)
def _clear_cloud_secrets_cache():
    cloud_secrets.clear_cache()
    yield
    cloud_secrets.clear_cache()


def test_get_secrets_provider_defaults_to_empty(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))
    assert get_secrets_provider(cfg) == ""


def test_get_secrets_provider_reads_and_normalizes_value(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = SSM\n")
    cfg = load_config(str(ini))
    assert get_secrets_provider(cfg) == "ssm"


def test_get_secrets_region_returns_none_when_unset(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))
    assert get_secrets_region(cfg) is None


def test_get_secrets_region_reads_value(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nregion = us-west-2\n")
    cfg = load_config(str(ini))
    assert get_secrets_region(cfg) == "us-west-2"


def test_get_secrets_ssm_prefix_defaults(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))
    assert get_secrets_ssm_prefix(cfg) == "/nyxgpt"


def test_get_secrets_secretsmanager_id_defaults(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))
    assert get_secrets_secretsmanager_id(cfg) == "nyxgpt"


def test_get_auth_api_key_reads_local_value_when_provider_unset(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[auth]\napi_key = local-secret\n")
    cfg = load_config(str(ini))
    assert get_auth_api_key(cfg) == "local-secret"


def test_get_auth_api_key_resolves_from_ssm_when_provider_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = ssm\n[auth]\napi_key = should-not-be-used\n")
    cfg = load_config(str(ini))

    monkeypatch.setattr(
        cloud_secrets,
        "resolve_secret",
        lambda provider, key, **kwargs: f"cloud-{provider}-{key}",
    )

    assert get_auth_api_key(cfg) == "cloud-ssm-auth_api_key"


def test_get_openai_api_key_resolves_from_secretsmanager_when_provider_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = secretsmanager\n[openai]\napi_key = should-not-be-used\n")
    cfg = load_config(str(ini))

    monkeypatch.setattr(
        cloud_secrets,
        "resolve_secret",
        lambda provider, key, **kwargs: f"cloud-{provider}-{key}",
    )

    assert get_openai_api_key(cfg) == "cloud-secretsmanager-openai_api_key"


def test_get_github_pat_resolves_from_cloud_when_provider_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = ssm\n[github]\npat = should-not-be-used\n")
    cfg = load_config(str(ini))

    monkeypatch.setattr(
        cloud_secrets,
        "resolve_secret",
        lambda provider, key, **kwargs: f"cloud-{provider}-{key}",
    )

    assert get_github_pat(cfg) == "cloud-ssm-github_pat"


def test_get_auth_api_key_returns_empty_not_local_value_when_cloud_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured provider that fails to resolve must not silently fall
    back to a config.ini value -- cloud deploys never populate that value,
    so the failure should surface as empty (and thus a locked-down/failing
    integration) rather than be masked."""
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = ssm\n[auth]\napi_key = should-not-be-used\n")
    cfg = load_config(str(ini))

    def _raise(provider, key, **kwargs):
        raise cloud_secrets.CloudSecretsError("boom")

    monkeypatch.setattr(cloud_secrets, "resolve_secret", _raise)

    assert get_auth_api_key(cfg) == ""


def test_get_secrets_functions_pass_configured_region_and_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        "[secrets]\nprovider = ssm\nregion = eu-west-1\nssm_prefix = /custom\n"
        "[auth]\napi_key = should-not-be-used\n",
    )
    cfg = load_config(str(ini))

    calls = []

    def _fake_resolve(provider, key, **kwargs):
        calls.append((provider, key, kwargs))
        return "cloud-value"

    monkeypatch.setattr(cloud_secrets, "resolve_secret", _fake_resolve)

    get_auth_api_key(cfg)

    assert calls == [
        (
            "ssm",
            "auth_api_key",
            {"region": "eu-west-1", "ssm_prefix": "/custom", "secretsmanager_id": "nyxgpt"},
        )
    ]


def test_validate_config_rejects_unknown_secrets_provider(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = vault\n")
    cfg = load_config(str(ini))
    errors = validate_config(cfg)
    assert any("secrets.provider" in e for e in errors)


def test_validate_config_accepts_known_secrets_providers(tmp_path: Path) -> None:
    for provider in ("", "ssm", "secretsmanager"):
        ini = tmp_path / f"config-{provider or 'empty'}.ini"
        _write(ini, f"[secrets]\nprovider = {provider}\n")
        cfg = load_config(str(ini))
        errors = validate_config(cfg)
        assert not any("secrets.provider" in e for e in errors)


def test_get_github_repo_owner_and_name_read_values(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[github]\nrepo_owner = dkblinux98\nrepo_name = nyxGPT\n")
    cfg = load_config(str(ini))
    assert get_github_repo_owner(cfg) == "dkblinux98"
    assert get_github_repo_name(cfg) == "nyxGPT"


def test_get_effective_config_summary_redacts_guided_secrets(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        "[openai]\napi_key = sk-abc123\n"
        "[github]\npat = ghp_abc123\n"
        "[monitoring]\nslack_bot_token = xoxb-abc123\n",
    )
    cfg = load_config(str(ini))

    summary = get_effective_config_summary(cfg)

    assert summary["openai.api_key"] == "***redacted***"
    assert summary["github.pat"] == "***redacted***"
    assert summary["monitoring.slack_bot_token"] == "***redacted***"
    assert "sk-abc123" not in str(summary)
    assert "ghp_abc123" not in str(summary)
    assert "xoxb-abc123" not in str(summary)


def test_get_effective_config_summary_guided_secrets_empty_stay_empty(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))

    summary = get_effective_config_summary(cfg)

    assert summary["openai.api_key"] == ""
    assert summary["github.pat"] == ""
    assert summary["monitoring.slack_bot_token"] == ""


# --- SECRETS_SYNC_MANIFEST (#3505) ---


def test_secrets_sync_manifest_keys_are_section_dot_key() -> None:
    for full_key in SECRETS_SYNC_MANIFEST:
        section, _, key = full_key.partition(".")
        assert section and key, f"malformed manifest key: {full_key!r}"


def test_secrets_sync_manifest_values_are_unique_actions_secret_names() -> None:
    names = list(SECRETS_SYNC_MANIFEST.values())
    assert len(names) == len(set(names))


def test_secrets_sync_manifest_names_are_upper_snake_case() -> None:
    for name in SECRETS_SYNC_MANIFEST.values():
        assert name == name.upper()
        assert " " not in name


def test_secrets_sync_manifest_does_not_include_the_pat_itself() -> None:
    # [github] pat authenticates the sync call -- it is never a sync *target*.
    assert "github.pat" not in SECRETS_SYNC_MANIFEST


def test_log_effective_config_logs_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ini = tmp_path / "config.ini"
    _write(ini, "[nyxgpt]\ndefault_model = llama3.1:8b\n")
    cfg = load_config(str(ini))

    with caplog.at_level(logging.INFO, logger="nyxgpt.config"):
        log_effective_config(cfg)

    records = [r for r in caplog.records if r.getMessage() == "Effective configuration"]
    assert records
    assert records[0].effective_config["tracing.enabled"] is True


# --- Credential redaction in operator-facing messages (#3837) ------------------
#
# Two CodeQL "clear-text logging of sensitive information" alerts (#115, #116)
# were dismissed as false positives on the grounds that no secret reaches the
# sink *today*. That is a claim about the current call graph, and it has to be
# re-checked on every future edit. These two hardenings remove the argument.


def test_redact_url_userinfo_removes_a_password() -> None:
    assert (
        redact_url_userinfo("http://alice:s3cr3t@ollama.internal:11434")
        == "http://***@ollama.internal:11434"
    )


def test_redact_url_userinfo_leaves_a_credential_free_url_alone() -> None:
    assert redact_url_userinfo("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


def test_redact_url_userinfo_does_not_touch_an_at_sign_in_a_path() -> None:
    """`[^/@]` cannot cross a path separator, so a path `@` is not userinfo."""
    assert redact_url_userinfo("https://host/pkg/@scope/name") == "https://host/pkg/@scope/name"


def test_validate_config_does_not_echo_a_password_in_the_base_url(tmp_path: Path) -> None:
    """A rejected `ollama.base_url` reaches stderr -- without its credential."""
    ini = tmp_path / "config.ini"
    _write(ini, "[ollama]\nbase_url = ftp://alice:s3cr3t@ollama.internal\n")
    cfg = load_config(str(ini))

    errors = validate_config(cfg)

    assert any("Invalid ollama.base_url" in err for err in errors)
    assert not any("s3cr3t" in err for err in errors)
    assert any("***@ollama.internal" in err for err in errors)


def test_cloud_secret_failure_log_omits_the_provider_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The once-per-key WARNING names the failure class, never its message.

    A cloud provider composes that message from whatever its SDK handed it;
    nothing in this process constrains it to be value-free.
    """
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = ssm\n[auth]\napi_key = should-not-be-used\n")
    cfg = load_config(str(ini))
    reset_fallback_warnings()

    def _raise(provider, key, **kwargs):
        raise cloud_secrets.CloudSecretsError("payload was {'auth_api_key': 'sk-live-leaked'}")

    monkeypatch.setattr(cloud_secrets, "resolve_secret", _raise)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.config"):
        assert get_auth_api_key(cfg) == ""

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the fallback must still be visible in the logs"
    assert not any("sk-live-leaked" in message for message in warnings)
    assert any("CloudSecretsError" in message for message in warnings)
    assert any("auth_api_key" in message for message in warnings)


def test_cloud_secret_failure_debug_line_does_not_relog_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#3837 (CodeQL #130): the DEBUG companion logs the key, not the provider.

    The #115 hardening (PR #3899) added this DEBUG line and passed `provider`
    to it, which opened a fresh `py/clear-text-logging-sensitive-data` alert
    -- the fix for one alert built the next. The provider is already named
    once per key by the WARNING above it, so dropping it here costs nothing;
    what the line exists for is the exception, and that must survive.
    """
    ini = tmp_path / "config.ini"
    _write(ini, "[secrets]\nprovider = ssm\n[auth]\napi_key = should-not-be-used\n")
    cfg = load_config(str(ini))
    reset_fallback_warnings()

    def _raise(provider, key, **kwargs):
        raise cloud_secrets.CloudSecretsError("payload was {'auth_api_key': 'sk-live-leaked'}")

    monkeypatch.setattr(cloud_secrets, "resolve_secret", _raise)

    with caplog.at_level(logging.DEBUG, logger="nyxgpt.config"):
        assert get_auth_api_key(cfg) == ""

    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug, "the opt-in DEBUG detail must still be emitted"
    assert not any("ssm" in r.getMessage() for r in debug)
    assert any("auth_api_key" in r.getMessage() for r in debug)
    # The exception is the whole point of the DEBUG line -- redacting the
    # provider must not have taken the traceback with it.
    assert any(r.exc_info is not None for r in debug)


# --- unparseable config.ini reports its real cause (#3944) ---
#
# `parser.read()` was completely unguarded, so a `configparser` error escaped
# `load_config` and the API's catch-all handler flattened it to "Internal
# server error". Every endpoint loads config, so one bad line took the entire
# dashboard down while stating no cause at all.


def test_load_config_raises_config_parse_error_naming_the_line(tmp_path: Path) -> None:
    from nyxgpt.config import ConfigParseError

    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[monitoring]
SLACK_BOT_TOKEN = a
slack_bot_token = b
""".lstrip(),
    )

    with pytest.raises(ConfigParseError) as excinfo:
        load_config(str(ini))

    message = str(excinfo.value)
    assert str(ini) in message
    assert "DuplicateOptionError" in message
    assert "line 3" in message
    assert "slack_bot_token" in message
    # The whole point: a user staring at two differently-cased lines is told
    # why they collide.
    assert "case-insensitive" in message


def test_load_config_parse_error_is_not_a_bare_configparser_error(tmp_path: Path) -> None:
    """Callers catch `ConfigParseError`; it must not also be a `configparser.Error`."""
    import configparser

    from nyxgpt.config import ConfigParseError

    ini = tmp_path / "config.ini"
    _write(ini, "not-a-header = 1\n")

    with pytest.raises(ConfigParseError) as excinfo:
        load_config(str(ini))
    assert not isinstance(excinfo.value, configparser.Error)
    assert isinstance(excinfo.value.__cause__, configparser.Error)


def test_describe_config_parse_error_covers_each_configparser_shape(tmp_path: Path) -> None:
    """The line number lives on a different attribute per subclass -- cover them."""
    import configparser

    from nyxgpt.config import describe_config_parse_error

    cases = {
        "[a]\nx = 1\n[a]\ny = 2\n": ("DuplicateSectionError", "line 3"),
        "x = 1\n[a]\n": ("MissingSectionHeaderError", "line 1"),
        "[a]\n x = 1\ny 2\n": ("ParsingError", "line 3"),
    }
    for text, (expected_type, expected_line) in cases.items():
        try:
            configparser.ConfigParser().read_string(text)
        except configparser.Error as e:
            rendered = describe_config_parse_error(tmp_path / "config.ini", e)
        else:  # pragma: no cover - the fixtures above are all invalid
            raise AssertionError(f"expected a parse error for {text!r}")
        assert expected_type in rendered
        assert expected_line in rendered
        assert "config.ini" in rendered


def test_describe_config_parse_error_redacts_the_offending_line_by_default() -> None:
    """The API returns this text pre-auth, so it must not quote the file (#3944).

    `config_unreadable_guard` is the outermost middleware by necessity --
    `api_key_auth` loads config before it can check a key, so a malformed
    config.ini is a state in which auth cannot be enforced at all. The two
    shapes that carried raw text are `MissingSectionHeaderError` (whose
    `.line` is the setting that appears before any header) and `ParsingError`
    (whose `.errors` pairs a line number with the line). A hand-edit that
    drops a `[section]` header makes a live credential the offender.
    """
    import configparser

    from nyxgpt.config import describe_config_parse_error

    secret = "sk-live-VERYSECRETVALUE"
    cases = [
        (f"api_key = {secret}\n[auth]\n", "MissingSectionHeaderError", "line 1"),
        (f"[auth]\napi_key = {secret}\n oops\nkey {secret}\n", "ParsingError", "line 4"),
    ]
    for text, expected_type, expected_line in cases:
        try:
            configparser.ConfigParser().read_string(text)
        except configparser.Error as e:
            rendered = describe_config_parse_error(Path("/home/u/.nyxGPT/config.ini"), e)
        else:  # pragma: no cover - the fixtures above are all invalid
            raise AssertionError(f"expected a parse error for {text!r}")

        # Still a diagnosis: the class and the line number survive redaction.
        assert expected_type in rendered
        assert expected_line in rendered
        # But no bytes from the file.
        assert secret not in rendered
        assert "api_key" not in rendered
        # And it points at the one surface that will show the line.
        assert "nyxgpt ops doctor" in rendered


def test_describe_config_parse_error_names_the_file_home_relative(monkeypatch) -> None:
    """The path is the same pre-auth channel on a smaller scale (#3944 review).

    `Cannot parse /Users/darla/.nyxGPT/config.ini: ...` reaches unauthenticated
    callers and spells out the OS account name. `~/.nyxGPT/config.ini` names
    the file just as usefully and names nobody. A path *outside* home is the
    operator's explicit choice and is left intact -- shortening it would only
    hide which file failed.
    """
    import configparser

    from nyxgpt.config import describe_config_parse_error

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/darla")))

    try:
        configparser.ConfigParser().read_string("[a]\nx = 1\nx = 2\n")
    except configparser.Error as e:
        exc: configparser.Error = e
    else:  # pragma: no cover - the fixture above is invalid
        raise AssertionError("expected a parse error")

    for include_line_text in (False, True):
        rendered = describe_config_parse_error(
            Path("/home/darla/.nyxGPT/config.ini"), exc, include_line_text=include_line_text
        )
        assert "~/.nyxGPT/config.ini" in rendered
        assert "/home/darla" not in rendered

    # Outside home: left exactly as given, so the operator can see which file.
    elsewhere = describe_config_parse_error(Path("/etc/nyxgpt/config.ini"), exc)
    assert "/etc/nyxgpt/config.ini" in elsewhere


def test_describe_config_parse_error_quotes_the_line_only_when_opted_in() -> None:
    """`nyxgpt ops doctor` is local and does opt in -- that split is the fix."""
    import configparser

    from nyxgpt.config import describe_config_parse_error

    secret = "sk-live-VERYSECRETVALUE"
    try:
        configparser.ConfigParser().read_string(f"api_key = {secret}\n[auth]\n")
    except configparser.Error as e:
        rendered = describe_config_parse_error(Path("config.ini"), e, include_line_text=True)
    else:  # pragma: no cover - the fixture above is invalid
        raise AssertionError("expected a parse error")

    assert secret in rendered
    assert "MissingSectionHeaderError" in rendered
    assert "line 1" in rendered


def test_describe_config_parse_error_redacts_the_fallback_shape() -> None:
    """The default branch renders `str(exc)`, and some subclasses put values there.

    `InterpolationMissingOptionError` carries the raw value it failed to
    interpolate. `read()` does not raise it (interpolation is lazy), but this
    helper is a general renderer and the redaction must not depend on a
    caller-by-caller judgement about which subclass is safe.
    """
    import configparser

    from nyxgpt.config import describe_config_parse_error

    exc = configparser.InterpolationMissingOptionError(
        "api_key", "auth", "sk-live-VERYSECRETVALUE-%(missing)s", "missing"
    )

    redacted = describe_config_parse_error(Path("config.ini"), exc)
    assert "sk-live-VERYSECRETVALUE" not in redacted
    assert "InterpolationMissingOptionError" in redacted

    assert "sk-live-VERYSECRETVALUE" in describe_config_parse_error(
        Path("config.ini"), exc, include_line_text=True
    )


def test_load_config_parse_error_does_not_carry_the_offending_line(tmp_path: Path) -> None:
    """End to end: what `load_config` raises is what the HTTP body says."""
    from nyxgpt.config import ConfigParseError

    ini = tmp_path / "config.ini"
    _write(ini, "api_key = sk-live-VERYSECRETVALUE\n[auth]\nenabled = true\n")

    with pytest.raises(ConfigParseError) as excinfo:
        load_config(str(ini))

    message = str(excinfo.value)
    assert "MissingSectionHeaderError" in message
    assert "line 1" in message
    assert "sk-live-VERYSECRETVALUE" not in message
