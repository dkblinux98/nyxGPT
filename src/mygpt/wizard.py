"""Interactive configuration wizard for myGPT setup.

This module provides an interactive CLI wizard that guides users through
the initial configuration of myGPT, including Ollama validation, RAG setup,
and config.ini generation.
"""

from __future__ import annotations

import sys
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from mygpt.config import DEFAULT_CONFIG_PATH
from mygpt.models import list_models


def _prompt(question: str, default: str | None = None) -> str:
    """Prompt user for input with optional default value."""
    if default:
        prompt_text = f"{question} [{default}]: "
    else:
        prompt_text = f"{question}: "

    try:
        value = input(prompt_text).strip()
        if not value and default:
            return default
        return value
    except (EOFError, KeyboardInterrupt):
        print("\n\nWizard cancelled.")
        sys.exit(0)


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    """Prompt user for yes/no response."""
    default_str = "Y/n" if default else "y/N"
    response = _prompt(f"{question} ({default_str})", "y" if default else "n")
    return response.lower() in {"y", "yes"}


def _validate_ollama_connection(
    base_url: str,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Validate connection to Ollama and return models if available.

    Returns:
        Tuple of (success, message, models_list)
    """
    print(f"\n🔍 Testing connection to Ollama at {base_url}...")

    try:
        models = list_models(base_url=base_url)
        if not models:
            return (
                False,
                "Connected to Ollama but no models found. Please pull a model first.",
                [],
            )

        print(f"✅ Successfully connected! Found {len(models)} model(s).")
        return True, "Success", models
    except Exception as e:
        error_msg = str(e)
        if "Failed to reach" in error_msg or "Connection refused" in error_msg:
            return False, "Cannot connect to Ollama. Make sure Ollama is running.", []
        return False, f"Ollama connection error: {error_msg}", []


def _select_model(models: list[dict[str, Any]], default: str = "qwen2.5:0.5b") -> str:
    """Prompt user to select a model from available models."""
    if not models:
        return default

    print("\n📦 Available models:")
    for i, model in enumerate(models, 1):
        name = model.get("name", "")
        size = model.get("size", 0)
        size_gb = size / (1024**3)
        print(f"  {i}. {name} ({size_gb:.1f} GB)")

    while True:
        response = _prompt(f"\nSelect a model (1-{len(models)})", "1")
        if not response:
            response = "1"

        try:
            idx = int(response) - 1
            if 0 <= idx < len(models):
                selected: str = models[idx].get("name", "")
                if selected:
                    return selected
        except ValueError:
            pass

        print(f"Invalid selection. Please enter a number between 1 and {len(models)}.")


def _configure_rag() -> dict[str, Any]:
    """Configure RAG settings through interactive prompts.

    Returns:
        Dictionary of RAG configuration options
    """
    print("\n📚 RAG (Retrieval-Augmented Generation) Configuration")
    print("RAG allows myGPT to use external documents as context for responses.")
    print("Note: RAG requires Docker Desktop and Cassandra to be running.")

    enable_rag = _prompt_yes_no("Enable RAG support?", default=False)

    rag_config: dict[str, Any] = {
        "enable_chat_context": enable_rag,
    }

    if enable_rag:
        print("\nUsing default RAG settings:")
        print("  - Cassandra host: 127.0.0.1:9042")
        print("  - Embedding model: nomic-embed-text")
        print("  - Chunk size: 800 tokens")
        print("  - Top-k retrieval: 5 chunks")

        customize = _prompt_yes_no("Customize RAG settings?", default=False)
        if customize:
            # Cassandra connection
            cassandra_host = _prompt("Cassandra host", "127.0.0.1")
            cassandra_port = _prompt("Cassandra port", "9042")

            # Embedding model
            embedding_model = _prompt("Embedding model", "nomic-embed-text")

            # Chunking
            chunk_size = _prompt("Chunk size (tokens)", "800")
            chunk_overlap = _prompt("Chunk overlap (tokens)", "100")

            # Retrieval
            chat_top_k = _prompt("Number of chunks to retrieve (top-k)", "5")
            max_chunks = _prompt("Maximum chunks to inject into prompt", "6")

            rag_config.update(
                {
                    "cassandra_hosts": cassandra_host,
                    "cassandra_port": cassandra_port,
                    "embedding_model": embedding_model,
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                    "chat_top_k": chat_top_k,
                    "max_chunks": max_chunks,
                }
            )

    return rag_config


def _generate_config_ini(
    output_path: Path,
    model: str,
    ollama_base_url: str,
    rag_config: dict[str, Any],
) -> None:
    """Generate config.ini file from wizard responses.

    Args:
        output_path: Path where config.ini should be written
        model: Selected default model
        ollama_base_url: Ollama base URL
        rag_config: RAG configuration dictionary
    """
    config = ConfigParser()

    # [mygpt] section
    config.add_section("mygpt")
    config.set("mygpt", "default_model", model)
    config.set("mygpt", "chat_timeout_seconds", "180")
    config.set("mygpt", "sessions_dir", "~/.myGPT/sessions")
    config.set("mygpt", "vectorstore_dir", "~/.myGPT/vectorstore")
    config.set("mygpt", "auto_summarize_enabled", "true")
    config.set("mygpt", "auto_summarize_after_messages", "5")
    config.set("mygpt", "auto_sync_filename", "true")

    # [logging] section
    config.add_section("logging")
    config.set("logging", "level", "INFO")
    config.set("logging", "dir", "~/.myGPT/logs")

    # [ollama] section
    config.add_section("ollama")
    config.set("ollama", "base_url", ollama_base_url)

    # [api] section
    config.add_section("api")
    config.set("api", "host", "127.0.0.1")
    config.set("api", "port", "8000")

    # [web] section
    config.add_section("web")
    config.set("web", "host", "127.0.0.1")
    config.set("web", "port", "3000")
    config.set("web", "api_base_url", "")

    # [auth] section
    config.add_section("auth")
    config.set("auth", "enabled", "false")
    config.set("auth", "api_key", "")
    config.set("auth", "header", "X-API-Key")

    # [rate_limit] section
    config.add_section("rate_limit")
    config.set("rate_limit", "enabled", "false")
    config.set("rate_limit", "requests_per_second", "10")
    config.set("rate_limit", "burst_size", "20")

    # [context] section
    config.add_section("context")
    config.set("context", "default_window_size", "8192")
    config.set("context", "warning_threshold", "0.8")

    # [rag] section
    config.add_section("rag")
    enable_rag = rag_config.get("enable_chat_context", False)
    config.set("rag", "enable_chat_context", "true" if enable_rag else "false")

    if enable_rag:
        config.set(
            "rag", "cassandra_hosts", rag_config.get("cassandra_hosts", "127.0.0.1")
        )
        config.set(
            "rag", "cassandra_port", str(rag_config.get("cassandra_port", "9042"))
        )
        config.set("rag", "cassandra_keyspace", "mygpt")
        config.set("rag", "cassandra_table", "rag_chunks")
        config.set(
            "rag",
            "embedding_model",
            rag_config.get("embedding_model", "nomic-embed-text"),
        )
        config.set("rag", "embedding_dim", "768")
        config.set("rag", "embedding_batch_size", "16")
        config.set("rag", "embedding_timeout_seconds", "120")
        config.set("rag", "chunk_size", str(rag_config.get("chunk_size", "800")))
        config.set("rag", "chunk_overlap", str(rag_config.get("chunk_overlap", "100")))
        config.set("rag", "chat_top_k", str(rag_config.get("chat_top_k", "5")))
        config.set("rag", "min_score", "0.0")
        config.set("rag", "max_chunks", str(rag_config.get("max_chunks", "6")))
        config.set("rag", "chat_context_max_chars", "2400")
        config.set("rag", "dedupe", "true")
        config.set("rag", "enable_query_expansion", "false")
        config.set("rag", "include_scores", "false")
        config.set("rag", "include_headers", "true")

    # [paths] section
    config.add_section("paths")
    config.set("paths", "repo_dir", "/path/to/myGPT")
    config.set("paths", "venv_python", "/path/to/myGPT/.venv/bin/python")
    config.set("paths", "node_bin", "/opt/homebrew/bin/node")
    config.set("paths", "npm_bin", "/opt/homebrew/bin/npm")

    # Write to file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        config.write(f)

    # Set permissions to 600 (owner read/write only)
    output_path.chmod(0o600)


def run_wizard(output_path: Path | None = None) -> int:
    """Run the interactive configuration wizard.

    Args:
        output_path: Optional output path for config.ini (defaults to ~/.myGPT/config.ini)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if output_path is None:
        output_path = DEFAULT_CONFIG_PATH

    print("=" * 60)
    print("🧙 myGPT Configuration Wizard")
    print("=" * 60)
    print("\nThis wizard will help you set up myGPT.")
    print(f"Configuration will be saved to: {output_path}")

    # Check if config already exists
    if output_path.exists():
        print(f"\n⚠️  Config file already exists: {output_path}")
        overwrite = _prompt_yes_no("Overwrite existing configuration?", default=False)
        if not overwrite:
            print("Wizard cancelled.")
            return 1

    print("\n" + "=" * 60)
    print("Step 1: Ollama Configuration")
    print("=" * 60)

    # Validate Ollama connection
    ollama_base_url = _prompt("Ollama base URL", "http://127.0.0.1:11434")

    success, message, models = _validate_ollama_connection(ollama_base_url)
    if not success:
        print(f"\n❌ {message}")
        print("\nPlease ensure Ollama is running:")
        print("  - Install: https://ollama.ai")
        print("  - Start: ollama serve")
        print("  - Pull a model: ollama pull qwen2.5:0.5b")
        return 1

    # Select model
    print("\n" + "=" * 60)
    print("Step 2: Model Selection")
    print("=" * 60)

    default_model = _select_model(models)
    print(f"\n✅ Selected model: {default_model}")

    # Configure RAG
    print("\n" + "=" * 60)
    print("Step 3: RAG Configuration")
    print("=" * 60)

    rag_config = _configure_rag()

    # Generate config
    print("\n" + "=" * 60)
    print("Step 4: Generating Configuration")
    print("=" * 60)

    try:
        _generate_config_ini(output_path, default_model, ollama_base_url, rag_config)
        print(f"\n✅ Configuration saved to: {output_path}")
        print("   Permissions: 600 (owner read/write only)")

        # Next steps
        print("\n" + "=" * 60)
        print("🎉 Setup Complete!")
        print("=" * 60)
        print("\nNext steps:")
        print("  1. Test your setup:")
        print("     mygpt info")
        print("  2. Start chatting:")
        print("     mygpt chat")
        print("  3. Launch the TUI:")
        print("     mygpt tui")
        print("  4. Start services:")
        print("     mygpt ops install")

        if rag_config.get("enable_chat_context"):
            print("\nRAG is enabled. To use RAG features:")
            print("  1. Start Cassandra: mygpt ops restart cassandra")
            print("  2. Ingest documents: mygpt rag ingest <doc_id> <file>")
            print("  3. Enable RAG in chat: Ctrl+R (TUI) or use WebUI toggle")

        return 0

    except Exception as e:
        print(f"\n❌ Failed to write configuration: {e}")
        return 1


__all__ = ["run_wizard"]
