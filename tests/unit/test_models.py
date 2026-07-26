"""Unit tests for models.py module."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nyxgpt import models


@pytest.mark.unit
def test_format_model_size_bytes():
    """Test format_model_size with bytes."""
    assert models.format_model_size(0) == "0.0 B"
    assert models.format_model_size(512) == "512.0 B"
    assert models.format_model_size(1023) == "1023.0 B"


@pytest.mark.unit
def test_format_model_size_kilobytes():
    """Test format_model_size with kilobytes."""
    assert models.format_model_size(1024) == "1.0 KB"
    assert models.format_model_size(1536) == "1.5 KB"
    assert models.format_model_size(2048) == "2.0 KB"


@pytest.mark.unit
def test_format_model_size_megabytes():
    """Test format_model_size with megabytes."""
    assert models.format_model_size(1024 * 1024) == "1.0 MB"
    assert models.format_model_size(1536 * 1024) == "1.5 MB"


@pytest.mark.unit
def test_format_model_size_gigabytes():
    """Test format_model_size with gigabytes."""
    assert models.format_model_size(1024 * 1024 * 1024) == "1.0 GB"
    assert models.format_model_size(4_700_000_000) == "4.4 GB"


@pytest.mark.unit
def test_format_model_size_negative():
    """Test format_model_size with negative values."""
    assert models.format_model_size(-100) == "0 B"


@pytest.mark.unit
@patch("nyxgpt.models.get_json")
@patch("nyxgpt.models.load_config")
@patch("nyxgpt.models.get_ollama_base_url")
def test_list_models(mock_get_url, mock_load_config, mock_get_json):
    """Test list_models function."""
    mock_get_url.return_value = "http://localhost:11434"
    mock_get_json.return_value = {
        "models": [
            {"name": "llama3.1:8b", "size": 4_700_000_000},
            {"name": "mistral:7b", "size": 4_100_000_000},
        ]
    }

    result = models.list_models()

    assert len(result) == 2
    assert result[0]["name"] == "llama3.1:8b"
    assert result[1]["name"] == "mistral:7b"
    mock_get_json.assert_called_once()


@pytest.mark.unit
@patch("nyxgpt.models.post_json")
def test_pull_model_without_progress(mock_post_json):
    """Test pull_model without progress callback."""
    mock_post_json.return_value = {"status": "success"}
    base_url = "http://localhost:11434"

    result = models.pull_model("llama3.1:8b", base_url=base_url, progress_callback=None)

    assert result["status"] == "success"
    mock_post_json.assert_called_once()
    args, kwargs = mock_post_json.call_args
    # Check the payload (second positional argument)
    assert "llama3.1:8b" in str(args[1])


@pytest.mark.unit
def test_pull_model_empty_name():
    """Test pull_model with empty model name."""
    with pytest.raises(ValueError, match="Model name cannot be empty"):
        models.pull_model("", base_url="http://localhost:11434")

    with pytest.raises(ValueError, match="Model name cannot be empty"):
        models.pull_model("   ", base_url="http://localhost:11434")


@pytest.mark.unit
@patch("nyxgpt.models.delete_json")
def test_delete_model(mock_delete_json):
    """Test delete_model function."""
    base_url = "http://localhost:11434"

    models.delete_model("llama3.1:8b", base_url=base_url)

    mock_delete_json.assert_called_once()
    args, kwargs = mock_delete_json.call_args
    # Check the payload (second positional argument)
    assert "llama3.1:8b" in str(args[1])


@pytest.mark.unit
def test_delete_model_empty_name():
    """Test delete_model with empty model name."""
    with pytest.raises(ValueError, match="Model name cannot be empty"):
        models.delete_model("", base_url="http://localhost:11434")

    with pytest.raises(ValueError, match="Model name cannot be empty"):
        models.delete_model("   ", base_url="http://localhost:11434")


@pytest.mark.unit
@patch("nyxgpt.models.post_json")
def test_show_model(mock_post_json):
    """Test show_model function."""
    mock_post_json.return_value = {
        "modelfile": "FROM llama3.1:8b",
        "parameters": "temperature 0.7",
        "template": "{{ .System }} {{ .Prompt }}",
    }
    base_url = "http://localhost:11434"

    result = models.show_model("llama3.1:8b", base_url=base_url)

    assert "modelfile" in result
    assert "parameters" in result
    assert "template" in result
    mock_post_json.assert_called_once()


@pytest.mark.unit
def test_show_model_empty_name():
    """Test show_model with empty model name."""
    with pytest.raises(ValueError, match="Model name cannot be empty"):
        models.show_model("", base_url="http://localhost:11434")

    with pytest.raises(ValueError, match="Model name cannot be empty"):
        models.show_model("   ", base_url="http://localhost:11434")


@pytest.mark.unit
def test_format_model_size_petabytes():
    """Test format_model_size falls through to the PB branch for huge sizes."""
    assert models.format_model_size(2 * 1024**5) == "2.0 PB"


@pytest.mark.unit
@patch("nyxgpt.models.post_json")
@patch("nyxgpt.models.get_ollama_base_url")
@patch("nyxgpt.models.load_config")
def test_pull_model_loads_base_url_from_config_when_none(
    mock_load_config, mock_get_url, mock_post_json
):
    """When base_url isn't given, pull_model must derive it from load_config()."""
    mock_load_config.return_value = "cfg-object"
    mock_get_url.return_value = "http://cfg-host:11434"
    mock_post_json.return_value = {"status": "success"}

    result = models.pull_model("llama3.1:8b", base_url=None, progress_callback=None)

    assert result["status"] == "success"
    mock_load_config.assert_called_once_with(None)
    mock_get_url.assert_called_once_with("cfg-object")
    args, kwargs = mock_post_json.call_args
    assert args[0] == "http://cfg-host:11434/api/pull"


@pytest.mark.unit
@patch("nyxgpt.models.delete_json")
@patch("nyxgpt.models.get_ollama_base_url")
@patch("nyxgpt.models.load_config")
def test_delete_model_loads_base_url_from_config_when_none(
    mock_load_config, mock_get_url, mock_delete_json
):
    """When base_url isn't given, delete_model must derive it from load_config()."""
    mock_load_config.return_value = "cfg-object"
    mock_get_url.return_value = "http://cfg-host:11434"

    models.delete_model("llama3.1:8b", base_url=None)

    mock_load_config.assert_called_once_with(None)
    mock_get_url.assert_called_once_with("cfg-object")
    args, kwargs = mock_delete_json.call_args
    assert args[0] == "http://cfg-host:11434/api/delete"


@pytest.mark.unit
@patch("nyxgpt.models.post_json")
@patch("nyxgpt.models.get_ollama_base_url")
@patch("nyxgpt.models.load_config")
def test_show_model_loads_base_url_from_config_when_none(
    mock_load_config, mock_get_url, mock_post_json
):
    """When base_url isn't given, show_model must derive it from load_config()."""
    mock_load_config.return_value = "cfg-object"
    mock_get_url.return_value = "http://cfg-host:11434"
    mock_post_json.return_value = {"modelfile": "FROM llama3.1:8b"}

    result = models.show_model("llama3.1:8b", base_url=None)

    assert result["modelfile"] == "FROM llama3.1:8b"
    mock_load_config.assert_called_once_with(None)
    mock_get_url.assert_called_once_with("cfg-object")
    args, kwargs = mock_post_json.call_args
    assert args[0] == "http://cfg-host:11434/api/show"
