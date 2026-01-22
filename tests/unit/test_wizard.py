"""Tests for configuration wizard."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from mygpt.wizard import (
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

    with patch("mygpt.wizard.list_models", return_value=mock_models):
        success, message, models = _validate_ollama_connection("http://127.0.0.1:11434")

        assert success is True
        assert message == "Success"
        assert models == mock_models


def test_validate_ollama_connection_no_models():
    """Test Ollama connection with no models available."""
    with patch("mygpt.wizard.list_models", return_value=[]):
        success, message, models = _validate_ollama_connection("http://127.0.0.1:11434")

        assert success is False
        assert "no models found" in message.lower()
        assert models == []


def test_validate_ollama_connection_failure():
    """Test Ollama connection failure."""
    with patch(
        "mygpt.wizard.list_models", side_effect=RuntimeError("Failed to reach Ollama")
    ):
        success, message, models = _validate_ollama_connection("http://127.0.0.1:11434")

        assert success is False
        assert "Cannot connect" in message
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
    assert "[mygpt]" in content
    assert "default_model = qwen2.5:0.5b" in content
    assert "[ollama]" in content
    assert "base_url = http://127.0.0.1:11434" in content
    assert "[rag]" in content
    assert "enable_chat_context = false" in content


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
    output_path.write_text("[mygpt]\ndefault_model = test\n")

    with patch("builtins.input", return_value="n"):
        exit_code = run_wizard(output_path=output_path)

        assert exit_code == 1

        captured = capsys.readouterr()
        assert "already exists" in captured.out
        assert "cancelled" in captured.out.lower()


def test_run_wizard_ollama_connection_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Test wizard exits gracefully when Ollama connection fails."""
    output_path = tmp_path / "config.ini"

    inputs = [
        "http://127.0.0.1:11434",  # Ollama URL
    ]

    with patch("builtins.input", side_effect=inputs):
        with patch(
            "mygpt.wizard.list_models",
            side_effect=RuntimeError("Failed to reach Ollama"),
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

    with patch("builtins.input", side_effect=inputs):
        with patch("mygpt.wizard.list_models", return_value=mock_models):
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


def test_run_wizard_success_with_rag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
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

    with patch("builtins.input", side_effect=inputs):
        with patch("mygpt.wizard.list_models", return_value=mock_models):
            exit_code = run_wizard(output_path=output_path)

            assert exit_code == 0

            captured = capsys.readouterr()
            assert "Setup Complete" in captured.out
            assert "RAG is enabled" in captured.out

            # Verify config file
            assert output_path.exists()
            content = output_path.read_text()
            assert "enable_chat_context = true" in content


def test_run_wizard_keyboard_interrupt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Test wizard handles keyboard interrupt gracefully."""
    output_path = tmp_path / "config.ini"

    with patch("builtins.input", side_effect=KeyboardInterrupt):
        with pytest.raises(SystemExit) as exc_info:
            run_wizard(output_path=output_path)

        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "cancelled" in captured.out.lower()
