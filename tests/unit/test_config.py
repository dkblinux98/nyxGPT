from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from mygpt.config import load_config, validate_config, get_api_port


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_load_config_defaults_exist() -> None:
    cfg = load_config(None)

    assert cfg.get("ollama", "base_url", fallback=None)
    assert cfg.get("mygpt", "default_model", fallback=None)


def test_load_config_from_explicit_path(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[mygpt]
default_model = llama3.1:8b

[ollama]
base_url = http://127.0.0.1:11434
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert cfg.get("mygpt", "default_model") == "llama3.1:8b"
    assert cfg.get("ollama", "base_url") == "http://127.0.0.1:11434"


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.ini"
    with pytest.raises(FileNotFoundError):
        load_config(str(missing))


def test_load_config_expands_tilde_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a home dir and a config file under it
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))

    ini = fake_home / ".myGPT" / "config.ini"
    _write(
        ini,
        """
[mygpt]
default_model = llama3.1:8b
""".lstrip(),
    )

    cfg = load_config("~/.myGPT/config.ini")
    assert cfg.get("mygpt", "default_model") == "llama3.1:8b"


def test_default_log_dir_is_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # This test encodes the desired default: ~/.myGPT/logs
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))

    cfg = load_config(None)
    # We will implement mygpt.log_dir; until then, this uses fallback.
    log_dir = cfg.get("mygpt", "log_dir", fallback=str(Path("~/.myGPT/logs").expanduser()))

    # Must resolve under fake HOME
    resolved = Path(log_dir).expanduser()
    assert str(resolved).startswith(str(fake_home))


def test_config_allows_overriding_log_dir(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        f"""
[mygpt]
log_dir = {tmp_path / 'logs'}
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert cfg.get("mygpt", "log_dir") == str(tmp_path / "logs")


def test_chat_timeout_default_can_be_read(tmp_path: Path) -> None:
    ini = tmp_path / "config.ini"
    _write(
        ini,
        """
[mygpt]
chat_timeout_seconds = 180
""".lstrip(),
    )

    cfg = load_config(str(ini))
    assert cfg.getint("mygpt", "chat_timeout_seconds") == 180


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


def test_get_api_port_handles_invalid_type_gracefully(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
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
    with caplog.at_level(logging.WARNING):
        port = get_api_port(cfg)

    assert port == 8000
    assert "Invalid api.port" in caplog.text
