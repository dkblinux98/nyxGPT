"""Tests for configuration wizard."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt.wizard import (
    _configure_rag,
    _generate_config_ini,
    _select_model,
    _validate_ollama_connection,
    run_wizard,
)

pytestmark = pytest.mark.unit


def test_validate_ollama_connection_success():
    """Test successful Ollama connection validation."""
    mock_models = [
        {"name": "llama3.1:8b", "size": 5000000000},
        {"name": "qwen2.5:0.5b", "size": 500000000},
    ]

    with patch("nyxgpt.wizard.list_models", return_value=mock_models):
        success, message, models = _validate_ollama_connection("http://127.0.0.1:11434")

        assert success is True
        assert message == "Success"
        assert models == mock_models


def test_validate_ollama_connection_no_models():
    """Test Ollama connection with no models available."""
    with patch("nyxgpt.wizard.list_models", return_value=[]):
        success, message, models = _validate_ollama_connection("http://127.0.0.1:11434")

        assert success is False
        assert "no models found" in message.lower()
        assert models == []


def test_validate_ollama_connection_failure():
    """Test Ollama connection failure."""
    with patch("nyxgpt.wizard.list_models", side_effect=RuntimeError("Failed to reach Ollama")):
        success, message, models = _validate_ollama_connection("http://127.0.0.1:11434")

        assert success is False
        assert "Cannot connect" in message
        assert models == []


def test_validate_ollama_connection_other_error():
    """A non-connection-refused exception is surfaced with its own message,
    not misreported as 'Ollama is not running'."""
    with patch("nyxgpt.wizard.list_models", side_effect=ValueError("unexpected 500 from Ollama")):
        success, message, models = _validate_ollama_connection("http://127.0.0.1:11434")

        assert success is False
        assert message == "Ollama connection error: unexpected 500 from Ollama"
        assert models == []


def test_select_model_with_models():
    """Test model selection from available models."""
    models = [
        {"name": "llama3.1:8b", "size": 5000000000},
        {"name": "qwen2.5:0.5b", "size": 500000000},
    ]

    with patch("builtins.input", return_value="2"):
        selected = _select_model(models, default="qwen2.5:0.5b")
        assert selected == "qwen2.5:0.5b"


def test_select_model_empty_list():
    """Test model selection with empty model list returns default."""
    models = []
    selected = _select_model(models, default="qwen2.5:0.5b")
    assert selected == "qwen2.5:0.5b"


def test_select_model_default_selection():
    """Test model selection with default (pressing enter)."""
    models = [
        {"name": "llama3.1:8b", "size": 5000000000},
        {"name": "qwen2.5:0.5b", "size": 500000000},
    ]

    with patch("builtins.input", return_value=""):
        selected = _select_model(models, default="qwen2.5:0.5b")
        assert selected == "llama3.1:8b"  # First model is selected by default


def test_select_model_blank_prompt_response_defaults_to_first():
    """If the low-level _prompt helper ever returns a falsy value despite a
    default being supplied (defensive branch), _select_model still falls
    back to choice '1' rather than crashing on int('')."""
    models = [
        {"name": "llama3.1:8b", "size": 5000000000},
        {"name": "qwen2.5:0.5b", "size": 500000000},
    ]

    with patch("nyxgpt.wizard._prompt", return_value=""):
        selected = _select_model(models, default="qwen2.5:0.5b")
        assert selected == "llama3.1:8b"


def test_select_model_reprompts_on_invalid_then_non_numeric_input():
    """Non-numeric input is rejected (caught ValueError) and reprompted;
    the user then enters a valid selection on the second attempt."""
    models = [
        {"name": "llama3.1:8b", "size": 5000000000},
        {"name": "qwen2.5:0.5b", "size": 500000000},
    ]

    with patch("builtins.input", side_effect=["not-a-number", "2"]):
        selected = _select_model(models, default="qwen2.5:0.5b")
        assert selected == "qwen2.5:0.5b"


def test_select_model_reprompts_on_out_of_range_selection():
    """A numeric but out-of-range selection is rejected and reprompted."""
    models = [
        {"name": "llama3.1:8b", "size": 5000000000},
        {"name": "qwen2.5:0.5b", "size": 500000000},
    ]

    with patch("builtins.input", side_effect=["99", "1"]):
        selected = _select_model(models, default="qwen2.5:0.5b")
        assert selected == "llama3.1:8b"


def test_configure_rag_disabled():
    """Test RAG configuration when disabled."""
    with patch("builtins.input", return_value="n"):
        rag_config = _configure_rag()

        assert rag_config["enable_chat_context"] is False
        assert "cassandra_hosts" not in rag_config


def test_configure_rag_enabled_defaults():
    """Test RAG configuration with defaults when enabled."""
    # First input: enable RAG (y), second input: don't customize (n)
    with patch("builtins.input", side_effect=["y", "n"]):
        rag_config = _configure_rag()

        assert rag_config["enable_chat_context"] is True
        # Should not have custom settings
        assert "cassandra_hosts" not in rag_config


def test_configure_rag_enabled_custom():
    """Test RAG configuration with custom settings."""
    inputs = [
        "y",  # Enable RAG
        "y",  # Customize settings
        "192.168.1.100",  # Cassandra host
        "9042",  # Cassandra port
        "nomic-embed-text",  # Embedding model
        "1000",  # Chunk size
        "150",  # Chunk overlap
        "10",  # Top-k
        "8",  # Max chunks
    ]

    with patch("builtins.input", side_effect=inputs):
        rag_config = _configure_rag()

        assert rag_config["enable_chat_context"] is True
        assert rag_config["cassandra_hosts"] == "192.168.1.100"
        assert rag_config["cassandra_port"] == "9042"
        assert rag_config["embedding_model"] == "nomic-embed-text"
        assert rag_config["chunk_size"] == "1000"
        assert rag_config["chunk_overlap"] == "150"
        assert rag_config["chat_top_k"] == "10"
        assert rag_config["max_chunks"] == "8"


def test_generate_config_ini_basic(tmp_path: Path):
    """Test config.ini generation with basic settings."""
    output_path = tmp_path / "config.ini"
    rag_config = {"enable_chat_context": False}

    _generate_config_ini(
        output_path=output_path,
        model="qwen2.5:0.5b",
        ollama_base_url="http://127.0.0.1:11434",
        rag_config=rag_config,
    )

    assert output_path.exists()

    # Check file permissions (should be 600)
    stat = output_path.stat()
    mode = stat.st_mode & 0o777
    assert mode == 0o600

    # Read and verify content
    content = output_path.read_text()
    assert "[nyxgpt]" in content
    assert "default_model = qwen2.5:0.5b" in content
    assert "[ollama]" in content
    assert "base_url = http://127.0.0.1:11434" in content
    assert "[rag]" in content
    assert "enable_chat_context = false" in content


def test_generate_config_ini_autodetects_paths(tmp_path: Path, monkeypatch):
    """[paths] is auto-detected from the running environment (#3388 rescue):
    the generated config must point at this checkout and interpreter, never
    at "/path/to/nyxGPT" placeholder text the user has to remember to edit."""
    import sys

    import nyxgpt.wizard as wizard_mod

    monkeypatch.setattr(wizard_mod.shutil, "which", lambda prog: f"/fake/bin/{prog}")

    output_path = tmp_path / "config.ini"
    _generate_config_ini(
        output_path=output_path,
        model="qwen2.5:0.5b",
        ollama_base_url="http://127.0.0.1:11434",
        rag_config={"enable_chat_context": False},
    )

    parser = ConfigParser()
    parser.read(output_path)

    expected_repo = str(Path(wizard_mod.__file__).resolve().parents[2])
    assert parser.get("paths", "repo_dir") == expected_repo
    assert parser.get("paths", "venv_python") == str(Path(sys.executable).resolve())
    assert parser.get("paths", "node_bin") == "/fake/bin/node"
    assert parser.get("paths", "npm_bin") == "/fake/bin/npm"
    assert "/path/to/nyxGPT" not in output_path.read_text()


def test_generate_config_ini_paths_fall_back_when_tools_missing(tmp_path: Path, monkeypatch):
    """When node/npm aren't on PATH, fall back to the standard Homebrew
    locations rather than writing empty values."""
    import nyxgpt.wizard as wizard_mod

    monkeypatch.setattr(wizard_mod.shutil, "which", lambda prog: None)

    output_path = tmp_path / "config.ini"
    _generate_config_ini(
        output_path=output_path,
        model="qwen2.5:0.5b",
        ollama_base_url="http://127.0.0.1:11434",
        rag_config={"enable_chat_context": False},
    )

    parser = ConfigParser()
    parser.read(output_path)
    assert parser.get("paths", "node_bin") == "/opt/homebrew/bin/node"
    assert parser.get("paths", "npm_bin") == "/opt/homebrew/bin/npm"


def test_generate_config_ini_generates_secrets(tmp_path: Path):
    """config.ini's auth.api_key and monitoring.grafana_admin_password are
    generated automatically, so config.ini is a ready-to-use single source
    of truth for these secrets (see #3194)."""
    output_path = tmp_path / "config.ini"
    rag_config = {"enable_chat_context": False}

    _generate_config_ini(
        output_path=output_path,
        model="qwen2.5:0.5b",
        ollama_base_url="http://127.0.0.1:11434",
        rag_config=rag_config,
    )

    parser = ConfigParser()
    parser.read(output_path)

    api_key = parser.get("auth", "api_key")
    grafana_password = parser.get("monitoring", "grafana_admin_password")

    assert api_key
    assert len(api_key) >= 32
    assert grafana_password
    assert len(grafana_password) >= 24
    # The two secrets must be independently generated, not copies of each other.
    assert api_key != grafana_password


def test_generate_config_ini_secrets_are_unique_per_run(tmp_path: Path):
    """Each call generates fresh random secrets rather than a fixed value."""
    rag_config = {"enable_chat_context": False}

    first_path = tmp_path / "first.ini"
    second_path = tmp_path / "second.ini"

    _generate_config_ini(
        output_path=first_path,
        model="qwen2.5:0.5b",
        ollama_base_url="http://127.0.0.1:11434",
        rag_config=rag_config,
    )
    _generate_config_ini(
        output_path=second_path,
        model="qwen2.5:0.5b",
        ollama_base_url="http://127.0.0.1:11434",
        rag_config=rag_config,
    )

    first = ConfigParser()
    first.read(first_path)
    second = ConfigParser()
    second.read(second_path)

    assert first.get("auth", "api_key") != second.get("auth", "api_key")
    assert first.get("monitoring", "grafana_admin_password") != second.get(
        "monitoring", "grafana_admin_password"
    )


def test_generate_config_ini_with_rag(tmp_path: Path):
    """Test config.ini generation with RAG enabled."""
    output_path = tmp_path / "config.ini"
    rag_config = {
        "enable_chat_context": True,
        "cassandra_hosts": "192.168.1.100",
        "cassandra_port": "9042",
        "embedding_model": "nomic-embed-text",
        "chunk_size": "1000",
        "chunk_overlap": "150",
        "chat_top_k": "10",
        "max_chunks": "8",
    }

    _generate_config_ini(
        output_path=output_path,
        model="llama3.1:8b",
        ollama_base_url="http://localhost:11434",
        rag_config=rag_config,
    )

    assert output_path.exists()

    content = output_path.read_text()
    assert "default_model = llama3.1:8b" in content
    assert "enable_chat_context = true" in content
    assert "cassandra_hosts = 192.168.1.100" in content
    assert "cassandra_port = 9042" in content
    assert "embedding_model = nomic-embed-text" in content
    assert "chunk_size = 1000" in content
    assert "chunk_overlap = 150" in content
    assert "chat_top_k = 10" in content
    assert "max_chunks = 8" in content


def test_run_wizard_cancelled_on_existing_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Test wizard exits when user refuses to overwrite existing config."""
    output_path = tmp_path / "config.ini"
    output_path.write_text("[nyxgpt]\ndefault_model = test\n")

    with patch("builtins.input", return_value="n"):
        exit_code = run_wizard(output_path=output_path)

        assert exit_code == 1

        captured = capsys.readouterr()
        assert "already exists" in captured.out
        assert "cancelled" in captured.out.lower()


def test_run_wizard_ollama_connection_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Test wizard exits gracefully when Ollama connection fails."""
    output_path = tmp_path / "config.ini"

    inputs = [
        "http://127.0.0.1:11434",  # Ollama URL
    ]

    with (
        patch("builtins.input", side_effect=inputs),
        patch(
            "nyxgpt.wizard.list_models",
            side_effect=RuntimeError("Failed to reach Ollama"),
        ),
    ):
        exit_code = run_wizard(output_path=output_path)

        assert exit_code == 1

        captured = capsys.readouterr()
        assert "Cannot connect" in captured.out


def test_run_wizard_success_minimal(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Test successful wizard run with minimal configuration."""
    output_path = tmp_path / "config.ini"

    mock_models = [
        {"name": "qwen2.5:0.5b", "size": 500000000},
        {"name": "llama3.1:8b", "size": 5000000000},
    ]

    inputs = [
        "http://127.0.0.1:11434",  # Ollama URL
        "1",  # Select first model
        "",  # System prompt (empty)
        "n",  # Disable RAG
    ]

    with (
        patch("builtins.input", side_effect=inputs),
        patch("nyxgpt.wizard.list_models", return_value=mock_models),
    ):
        exit_code = run_wizard(output_path=output_path)

        assert exit_code == 0

        captured = capsys.readouterr()
        assert "Setup Complete" in captured.out
        assert str(output_path) in captured.out

        # Verify config file was created
        assert output_path.exists()
        content = output_path.read_text()
        assert "default_model = qwen2.5:0.5b" in content
        assert "enable_chat_context = false" in content


def test_run_wizard_success_with_rag(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Test successful wizard run with RAG enabled."""
    output_path = tmp_path / "config.ini"

    mock_models = [{"name": "qwen2.5:0.5b", "size": 500000000}]

    inputs = [
        "http://127.0.0.1:11434",  # Ollama URL
        "",  # Select first model (default)
        "",  # System prompt (empty)
        "y",  # Enable RAG
        "n",  # Don't customize RAG settings
    ]

    with (
        patch("builtins.input", side_effect=inputs),
        patch("nyxgpt.wizard.list_models", return_value=mock_models),
    ):
        exit_code = run_wizard(output_path=output_path)

        assert exit_code == 0

        captured = capsys.readouterr()
        assert "Setup Complete" in captured.out
        assert "RAG is enabled" in captured.out

        # Verify config file
        assert output_path.exists()
        content = output_path.read_text()
        assert "enable_chat_context = true" in content


def test_run_wizard_uses_default_config_path_when_none_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """When output_path is omitted, the wizard falls back to
    nyxgpt.wizard.DEFAULT_CONFIG_PATH rather than requiring a caller-supplied
    path."""
    default_path = tmp_path / "default-config.ini"

    mock_models = [{"name": "qwen2.5:0.5b", "size": 500000000}]
    inputs = [
        "http://127.0.0.1:11434",  # Ollama URL
        "1",  # Select first model
        "",  # System prompt (empty)
        "n",  # Disable RAG
    ]

    with (
        patch("nyxgpt.wizard.DEFAULT_CONFIG_PATH", default_path),
        patch("builtins.input", side_effect=inputs),
        patch("nyxgpt.wizard.list_models", return_value=mock_models),
    ):
        exit_code = run_wizard()

        assert exit_code == 0
        assert default_path.exists()

        captured = capsys.readouterr()
        assert str(default_path) in captured.out


def test_run_wizard_reports_non_empty_system_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """A non-empty system prompt is echoed back (truncated) to confirm it
    was captured, rather than silently falling through the 'no prompt'
    branch."""
    output_path = tmp_path / "config.ini"

    mock_models = [{"name": "qwen2.5:0.5b", "size": 500000000}]
    inputs = [
        "http://127.0.0.1:11434",  # Ollama URL
        "1",  # Select first model
        "You are a helpful assistant.",  # System prompt
        "n",  # Disable RAG
    ]

    with (
        patch("builtins.input", side_effect=inputs),
        patch("nyxgpt.wizard.list_models", return_value=mock_models),
    ):
        exit_code = run_wizard(output_path=output_path)

        assert exit_code == 0

        captured = capsys.readouterr()
        assert "System prompt set: You are a helpful assistant." in captured.out


def test_run_wizard_reports_error_when_config_generation_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """If writing config.ini fails (e.g. permission error), the wizard
    catches it, reports the failure, and exits with code 1 instead of
    propagating the exception."""
    output_path = tmp_path / "config.ini"

    mock_models = [{"name": "qwen2.5:0.5b", "size": 500000000}]
    inputs = [
        "http://127.0.0.1:11434",  # Ollama URL
        "1",  # Select first model
        "",  # System prompt (empty)
        "n",  # Disable RAG
    ]

    with (
        patch("builtins.input", side_effect=inputs),
        patch("nyxgpt.wizard.list_models", return_value=mock_models),
        patch(
            "nyxgpt.wizard._generate_config_ini",
            side_effect=OSError("disk full"),
        ),
    ):
        exit_code = run_wizard(output_path=output_path)

        assert exit_code == 1

        captured = capsys.readouterr()
        assert "Failed to write configuration" in captured.out
        assert "disk full" in captured.out


def test_run_wizard_keyboard_interrupt(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Test wizard handles keyboard interrupt gracefully."""
    output_path = tmp_path / "config.ini"

    with patch("builtins.input", side_effect=KeyboardInterrupt):
        with pytest.raises(SystemExit) as exc_info:
            run_wizard(output_path=output_path)

        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "cancelled" in captured.out.lower()
