"""Integration tests for embedding and response caching."""

from unittest.mock import patch

from nyxgpt.chat import chat, clear_response_cache
from nyxgpt.rag.embeddings import clear_embedding_cache, embed_texts


class TestEmbeddingCache:
    """Test embedding caching functionality."""

    def setup_method(self):
        """Reset cache before each test."""
        clear_embedding_cache()

    def test_embedding_cache_disabled_by_default(self, tmp_path):
        """Test that caching is disabled by default in config."""
        config_content = """
[nyxgpt]
default_model = test-model

[ollama]
base_url = http://127.0.0.1:11434

[rag]
embedding_model = test-embed
embedding_dim = 768

[cache]
embedding_cache_enabled = false
"""
        config_file = tmp_path / "config.ini"
        config_file.write_text(config_content)

        # Mock the Ollama API call
        mock_response = {"embeddings": [[0.1] * 768, [0.2] * 768]}

        with (
            patch("nyxgpt.rag.embeddings._post_json", return_value=mock_response),
            patch("nyxgpt.rag.embeddings.load_config") as mock_load,
        ):
            from configparser import ConfigParser

            cfg = ConfigParser()
            cfg.read_string(config_content)
            mock_load.return_value = cfg

            # Clear cache to force re-initialization
            from nyxgpt.rag import embeddings

            embeddings._embedding_cache = None

            # First call
            result1 = embed_texts(["test1", "test2"])

            # Second call with same input
            result2 = embed_texts(["test1", "test2"])

            # Should have called API twice (no caching)
            assert len(result1) == 2
            assert len(result2) == 2

    def test_embedding_cache_memory_backend(self, tmp_path):
        """Test memory cache backend for embeddings."""
        config_content = """
[nyxgpt]
default_model = test-model

[ollama]
base_url = http://127.0.0.1:11434

[rag]
embedding_model = test-embed
embedding_dim = 768

[cache]
embedding_cache_enabled = true
embedding_cache_backend = memory
embedding_cache_max_size = 100
embedding_cache_ttl_seconds = 3600
"""
        config_file = tmp_path / "config.ini"
        config_file.write_text(config_content)

        # Mock the Ollama API call
        call_count = 0

        def mock_post_json(url, payload, timeout):
            nonlocal call_count
            call_count += 1
            num_texts = len(payload["input"])
            return {"embeddings": [[0.1] * 768 for _ in range(num_texts)]}

        with (
            patch("nyxgpt.rag.embeddings._post_json", side_effect=mock_post_json),
            patch("nyxgpt.rag.embeddings.load_config") as mock_load,
        ):
            from configparser import ConfigParser

            cfg = ConfigParser()
            cfg.read_string(config_content)
            mock_load.return_value = cfg

            # Clear cache to force re-initialization
            from nyxgpt.rag import embeddings

            embeddings._embedding_cache = None

            # First call - should hit API
            result1 = embed_texts(["test1", "test2"])
            assert len(result1) == 2
            assert call_count == 1

            # Second call with same input - should use cache
            result2 = embed_texts(["test1", "test2"])
            assert len(result2) == 2
            assert call_count == 1  # No additional API call

            # Different input - should hit API
            result3 = embed_texts(["test3"])
            assert len(result3) == 1
            assert call_count == 2  # New API call

    def test_embedding_cache_invalidation(self, tmp_path):
        """Test that clearing cache forces re-computation."""
        config_content = """
[nyxgpt]
default_model = test-model

[ollama]
base_url = http://127.0.0.1:11434

[rag]
embedding_model = test-embed
embedding_dim = 768

[cache]
embedding_cache_enabled = true
embedding_cache_backend = memory
"""
        call_count = 0

        def mock_post_json(url, payload, timeout):
            nonlocal call_count
            call_count += 1
            num_texts = len(payload["input"])
            return {"embeddings": [[0.1] * 768 for _ in range(num_texts)]}

        with (
            patch("nyxgpt.rag.embeddings._post_json", side_effect=mock_post_json),
            patch("nyxgpt.rag.embeddings.load_config") as mock_load,
        ):
            from configparser import ConfigParser

            cfg = ConfigParser()
            cfg.read_string(config_content)
            mock_load.return_value = cfg

            # Clear cache to force re-initialization
            from nyxgpt.rag import embeddings

            embeddings._embedding_cache = None

            # First call
            _ = embed_texts(["test1"])
            assert call_count == 1

            # Second call - cached
            _ = embed_texts(["test1"])
            assert call_count == 1

            # Clear cache
            clear_embedding_cache()

            # Third call - should hit API again
            _ = embed_texts(["test1"])
            assert call_count == 2


class TestResponseCache:
    """Test response caching functionality."""

    def setup_method(self):
        """Reset cache before each test."""
        clear_response_cache()

    def test_response_cache_disabled_by_default(self, tmp_path):
        """Test that response caching is disabled by default."""
        config_content = f"""
[nyxgpt]
default_model = test-model
sessions_dir = {tmp_path}/sessions

[ollama]
base_url = http://127.0.0.1:11434

[cache]
response_cache_enabled = false
"""

        config_file = tmp_path / "config.ini"
        config_file.write_text(config_content)

        call_count = 0

        def mock_ollama_chat(base_url, model, messages, timeout_s, output_format=None, **_kw):
            nonlocal call_count
            call_count += 1
            return "Test response"

        with (
            patch("nyxgpt.chat.ollama_chat", side_effect=mock_ollama_chat),
            patch("nyxgpt.chat.load_config") as mock_load,
        ):
            from configparser import ConfigParser

            cfg = ConfigParser()
            cfg.read_string(config_content)
            mock_load.return_value = cfg

            # Clear cache to force re-initialization
            from nyxgpt import chat as chat_module

            chat_module._response_cache = None

            # Mock session operations
            with (
                patch("nyxgpt.chat.load_session"),
                patch("nyxgpt.chat.save_session"),
            ):
                # First call
                chat("test prompt", config_path=str(config_file), new=True)
                assert call_count == 1

                # Second call with same prompt
                chat("test prompt", config_path=str(config_file), new=True)
                assert call_count == 2  # No caching

    def test_response_cache_memory_backend(self, tmp_path):
        """Test memory cache backend for responses."""
        config_content = f"""
[nyxgpt]
default_model = test-model
sessions_dir = {tmp_path}/sessions

[ollama]
base_url = http://127.0.0.1:11434

[cache]
response_cache_enabled = true
response_cache_backend = memory
response_cache_max_size = 50
response_cache_ttl_seconds = 1800
"""

        config_file = tmp_path / "config.ini"
        config_file.write_text(config_content)

        call_count = 0

        def mock_ollama_chat(base_url, model, messages, timeout_s, output_format=None, **_kw):
            nonlocal call_count
            call_count += 1
            return f"Response {call_count}"

        with (
            patch("nyxgpt.chat.ollama_chat", side_effect=mock_ollama_chat),
            patch("nyxgpt.chat.load_config") as mock_load,
        ):
            from configparser import ConfigParser

            cfg = ConfigParser()
            cfg.read_string(config_content)
            mock_load.return_value = cfg

            # Clear cache to force re-initialization
            from nyxgpt import chat as chat_module

            chat_module._response_cache = None

            # Mock session operations
            with (
                patch("nyxgpt.chat.load_session"),
                patch("nyxgpt.chat.save_session"),
            ):
                # First call
                result1 = chat("test prompt", config_path=str(config_file), new=True)
                assert call_count == 1
                assert result1.reply == "Response 1"

                # Second call with same prompt - should use cache
                result2 = chat("test prompt", config_path=str(config_file), new=True)
                assert call_count == 1  # No additional API call
                assert result2.reply == "Response 1"  # Cached response

    def test_response_cache_different_prompts(self, tmp_path):
        """Test that different prompts don't share cache entries."""
        config_content = f"""
[nyxgpt]
default_model = test-model
sessions_dir = {tmp_path}/sessions

[ollama]
base_url = http://127.0.0.1:11434

[cache]
response_cache_enabled = true
response_cache_backend = memory
"""

        config_file = tmp_path / "config.ini"
        config_file.write_text(config_content)

        call_count = 0

        def mock_ollama_chat(base_url, model, messages, timeout_s, output_format=None, **_kw):
            nonlocal call_count
            call_count += 1
            return f"Response {call_count}"

        with (
            patch("nyxgpt.chat.ollama_chat", side_effect=mock_ollama_chat),
            patch("nyxgpt.chat.load_config") as mock_load,
        ):
            from configparser import ConfigParser

            cfg = ConfigParser()
            cfg.read_string(config_content)
            mock_load.return_value = cfg

            # Clear cache to force re-initialization
            from nyxgpt import chat as chat_module

            chat_module._response_cache = None

            with (
                patch("nyxgpt.chat.load_session"),
                patch("nyxgpt.chat.save_session"),
            ):
                # First prompt
                chat("prompt1", config_path=str(config_file), new=True)
                assert call_count == 1

                # Different prompt
                chat("prompt2", config_path=str(config_file), new=True)
                assert call_count == 2  # New API call

                # Repeat first prompt - should use cache
                chat("prompt1", config_path=str(config_file), new=True)
                assert call_count == 2  # No new API call
