from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nyxgpt.config import (
    get_api_port,
    get_prompt_mode_enabled,
    get_prompt_mode_long_threshold,
    get_prompt_mode_short_threshold,
    get_rag_good_score_threshold,
    get_rag_medium_score_threshold,
    load_config,
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
