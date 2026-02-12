"""Tests for batching configuration."""

from configparser import ConfigParser

from nyxgpt.config import (
    get_batching_enabled,
    get_batching_max_queue_size,
    get_chat_batch_size,
    get_chat_batch_timeout_ms,
    get_rag_query_batch_size,
    get_rag_query_batch_timeout_ms,
    validate_config,
)


class TestBatchingConfigValidation:
    """Test configuration validation for batching settings."""

    def test_valid_batching_config(self):
        """Test validation passes for valid batching config."""
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.add_section("ollama")
        cfg.add_section("batching")
        cfg.set("batching", "enabled", "true")
        cfg.set("batching", "chat_batch_size", "5")
        cfg.set("batching", "chat_batch_timeout_ms", "100")
        cfg.set("batching", "rag_query_batch_size", "5")
        cfg.set("batching", "rag_query_batch_timeout_ms", "100")
        cfg.set("batching", "max_queue_size", "100")

        errors = validate_config(cfg)
        assert len(errors) == 0

    def test_chat_batch_size_out_of_range(self):
        """Test validation fails for chat_batch_size out of range."""
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.add_section("ollama")
        cfg.add_section("batching")
        cfg.set("batching", "chat_batch_size", "0")  # Below minimum

        errors = validate_config(cfg)
        assert any("chat_batch_size" in err and "must be 1-100" in err for err in errors)

        cfg.set("batching", "chat_batch_size", "101")  # Above maximum
        errors = validate_config(cfg)
        assert any("chat_batch_size" in err and "must be 1-100" in err for err in errors)

    def test_chat_batch_timeout_out_of_range(self):
        """Test validation fails for chat_batch_timeout_ms out of range."""
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.add_section("ollama")
        cfg.add_section("batching")
        cfg.set("batching", "chat_batch_timeout_ms", "5")  # Below minimum

        errors = validate_config(cfg)
        assert any("chat_batch_timeout_ms" in err and "must be 10-5000" in err for err in errors)

        cfg.set("batching", "chat_batch_timeout_ms", "6000")  # Above maximum
        errors = validate_config(cfg)
        assert any("chat_batch_timeout_ms" in err and "must be 10-5000" in err for err in errors)

    def test_rag_query_batch_size_out_of_range(self):
        """Test validation fails for rag_query_batch_size out of range."""
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.add_section("ollama")
        cfg.add_section("batching")
        cfg.set("batching", "rag_query_batch_size", "0")  # Below minimum

        errors = validate_config(cfg)
        assert any("rag_query_batch_size" in err and "must be 1-50" in err for err in errors)

        cfg.set("batching", "rag_query_batch_size", "51")  # Above maximum
        errors = validate_config(cfg)
        assert any("rag_query_batch_size" in err and "must be 1-50" in err for err in errors)

    def test_rag_query_timeout_out_of_range(self):
        """Test validation fails for rag_query_batch_timeout_ms out of range."""
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.add_section("ollama")
        cfg.add_section("batching")
        cfg.set("batching", "rag_query_batch_timeout_ms", "5")  # Below minimum

        errors = validate_config(cfg)
        assert any("rag_query_batch_timeout_ms" in err and "must be 10-5000" in err for err in errors)

    def test_max_queue_size_out_of_range(self):
        """Test validation fails for max_queue_size out of range."""
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.add_section("ollama")
        cfg.add_section("batching")
        cfg.set("batching", "max_queue_size", "5")  # Below minimum

        errors = validate_config(cfg)
        assert any("max_queue_size" in err and "must be 10-1000" in err for err in errors)

    def test_invalid_integer_values(self):
        """Test validation fails for non-integer values."""
        cfg = ConfigParser()
        cfg.add_section("nyxgpt")
        cfg.add_section("ollama")
        cfg.add_section("batching")
        cfg.set("batching", "chat_batch_size", "not_a_number")

        errors = validate_config(cfg)
        assert any("chat_batch_size" in err and "must be an integer" in err for err in errors)


class TestBatchingConfigGetters:
    """Test batching configuration getter functions."""

    def test_get_batching_enabled_default(self):
        """Test default value for batching enabled."""
        cfg = ConfigParser()
        cfg.add_section("batching")

        assert get_batching_enabled(cfg) is False

    def test_get_batching_enabled_true(self):
        """Test batching enabled when configured."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "enabled", "true")

        assert get_batching_enabled(cfg) is True

    def test_get_batching_enabled_false(self):
        """Test batching disabled when configured."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "enabled", "false")

        assert get_batching_enabled(cfg) is False

    def test_get_chat_batch_size_default(self):
        """Test default chat batch size."""
        cfg = ConfigParser()
        cfg.add_section("batching")

        assert get_chat_batch_size(cfg) == 5

    def test_get_chat_batch_size_custom(self):
        """Test custom chat batch size."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "chat_batch_size", "10")

        assert get_chat_batch_size(cfg) == 10

    def test_get_chat_batch_timeout_ms_default(self):
        """Test default chat batch timeout."""
        cfg = ConfigParser()
        cfg.add_section("batching")

        assert get_chat_batch_timeout_ms(cfg) == 100

    def test_get_chat_batch_timeout_ms_custom(self):
        """Test custom chat batch timeout."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "chat_batch_timeout_ms", "200")

        assert get_chat_batch_timeout_ms(cfg) == 200

    def test_get_rag_query_batch_size_default(self):
        """Test default RAG query batch size."""
        cfg = ConfigParser()
        cfg.add_section("batching")

        assert get_rag_query_batch_size(cfg) == 5

    def test_get_rag_query_batch_size_custom(self):
        """Test custom RAG query batch size."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "rag_query_batch_size", "10")

        assert get_rag_query_batch_size(cfg) == 10

    def test_get_rag_query_batch_timeout_ms_default(self):
        """Test default RAG query batch timeout."""
        cfg = ConfigParser()
        cfg.add_section("batching")

        assert get_rag_query_batch_timeout_ms(cfg) == 100

    def test_get_rag_query_batch_timeout_ms_custom(self):
        """Test custom RAG query batch timeout."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "rag_query_batch_timeout_ms", "250")

        assert get_rag_query_batch_timeout_ms(cfg) == 250

    def test_get_batching_max_queue_size_default(self):
        """Test default max queue size."""
        cfg = ConfigParser()
        cfg.add_section("batching")

        assert get_batching_max_queue_size(cfg) == 100

    def test_get_batching_max_queue_size_custom(self):
        """Test custom max queue size."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "max_queue_size", "200")

        assert get_batching_max_queue_size(cfg) == 200

    def test_invalid_value_fallback(self):
        """Test fallback to default when value is invalid."""
        cfg = ConfigParser()
        cfg.add_section("batching")
        cfg.set("batching", "chat_batch_size", "not_a_number")

        # Should fallback to default
        assert get_chat_batch_size(cfg) == 5
