"""Tests for multi-model RAG CLI commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

pytestmark = pytest.mark.unit


def test_rag_ingest_with_collection_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test rag ingest command with --collection flag."""
    from nyxgpt.cli import cli

    test_file = tmp_path / "test.txt"
    test_file.write_text("Test document content for embedding.")

    with patch("nyxgpt.cli.ingest_document") as mock_ingest:
        mock_ingest.return_value = {
            "status": "ingested",
            "chunks_ingested": 5,
            "doc_hash": "abc123",
            "previous_hash": None,
        }

        exit_code = cli(
            [
                "rag",
                "ingest",
                "test-doc",
                str(test_file),
                "--collection",
                "all-minilm",
                "--model",
                "all-minilm:latest",
                "--dimension",
                "384",
            ]
        )

        assert exit_code == 0
        mock_ingest.assert_called_once()
        args, kwargs = mock_ingest.call_args
        assert args[0] == "test-doc"
        assert kwargs["collection"] == "all-minilm"
        assert kwargs["embedding_model"] == "all-minilm:latest"
        assert kwargs["embedding_dim"] == 384

        captured = capsys.readouterr()
        assert "Ingested 5 chunks" in captured.out
        assert "all-minilm" in captured.out


def test_rag_query_with_collection_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag query command with --collection flag."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.retrieve_context") as mock_retrieve:
        mock_retrieve.return_value = [
            {"text": "Result 1", "score": 0.9, "embedding_model": "all-minilm:latest"},
            {"text": "Result 2", "score": 0.8, "embedding_model": "all-minilm:latest"},
        ]

        exit_code = cli(
            [
                "rag",
                "query",
                "test question",
                "--collection",
                "all-minilm",
                "--model",
                "all-minilm:latest",
                "--dimension",
                "384",
            ]
        )

        assert exit_code == 0
        mock_retrieve.assert_called_once()
        args, kwargs = mock_retrieve.call_args
        assert args[0] == "test question"
        assert kwargs["collection"] == "all-minilm"
        assert kwargs["embedding_model"] == "all-minilm:latest"
        assert kwargs["embedding_dim"] == 384

        captured = capsys.readouterr()
        assert "Results: 2" in captured.out
        assert "all-minilm" in captured.out


def test_rag_list_with_collection_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag list command with --collection flag."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        mock_store.list_docs.return_value = [
            {"doc_id": "doc1", "chunks": 10, "embedding_model": "all-minilm:latest"},
            {"doc_id": "doc2", "chunks": 5, "embedding_model": "all-minilm:latest"},
        ]
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "list", "--collection", "all-minilm"])

        assert exit_code == 0
        MockStore.assert_called_once_with(collection="all-minilm")
        mock_store.list_docs.assert_called_once()
        mock_store.close.assert_called_once()

        captured = capsys.readouterr()
        assert "Collection: all-minilm" in captured.out
        assert "doc1" in captured.out
        assert "doc2" in captured.out


def test_rag_collections_command(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag collections command."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        mock_store.list_collections.return_value = ["default", "all-minilm", "nomic768"]
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "collections"])

        assert exit_code == 0
        mock_store.list_collections.assert_called_once()
        mock_store.close.assert_called_once()

        captured = capsys.readouterr()
        assert "Available collections:" in captured.out
        assert "default" in captured.out
        assert "all-minilm" in captured.out
        assert "nomic768" in captured.out


def test_rag_compare_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag compare command."""
    from nyxgpt.cli import cli

    test_file = tmp_path / "test.txt"
    test_file.write_text("This is a test document. It has multiple sentences. Used for testing.")

    # Mock the imports from model_compare module where they're used
    with (
        patch("nyxgpt.rag.model_compare.compare_models") as mock_compare,
        patch("nyxgpt.rag.model_compare.print_comparison_table") as mock_print_table,
    ):
        from nyxgpt.rag.model_compare import ModelPerformanceMetrics

        mock_compare.return_value = [
            ModelPerformanceMetrics("nomic-embed-text", 768, 10.5, 5.2, None, None),
            ModelPerformanceMetrics("all-minilm-latest", 384, 8.3, 4.1, None, None),
        ]

        exit_code = cli(
            [
                "rag",
                "compare",
                str(test_file),
                "nomic-embed-text:768:default",
                "all-minilm-latest:384:all-minilm",
            ]
        )

        assert exit_code == 0
        mock_compare.assert_called_once()
        args = mock_compare.call_args[0]
        models = args[0]
        assert len(models) == 2
        assert models[0] == ("nomic-embed-text", 768, "default")
        assert models[1] == ("all-minilm-latest", 384, "all-minilm")

        mock_print_table.assert_called_once()


def test_rag_compare_invalid_spec(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag compare with invalid model spec."""
    import tempfile
    from pathlib import Path

    from nyxgpt.cli import cli

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("test")
        test_file = Path(f.name)

    try:
        exit_code = cli(
            [
                "rag",
                "compare",
                str(test_file),
                "invalid-spec",  # Missing : separators
            ]
        )

        assert exit_code == 2
        captured = capsys.readouterr()
        assert "Invalid model spec" in captured.err
    finally:
        test_file.unlink()


def test_rag_delete_with_collection_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag delete command with --collection flag."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "delete", "test-doc", "--collection", "all-minilm"])

        assert exit_code == 0
        MockStore.assert_called_once_with(collection="all-minilm")
        mock_store.delete_doc.assert_called_once_with("test-doc")
        mock_store.close.assert_called_once()

        captured = capsys.readouterr()
        assert "Deleted RAG document: test-doc" in captured.out
        assert "all-minilm" in captured.out


def test_rag_wipe_with_collection_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag wipe command with --collection flag."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "wipe", "--yes-really", "--collection", "all-minilm"])

        assert exit_code == 0
        MockStore.assert_called_once_with(collection="all-minilm")
        mock_store.truncate.assert_called_once()
        mock_store.close.assert_called_once()

        captured = capsys.readouterr()
        assert "Wiped all RAG documents" in captured.out
        assert "all-minilm" in captured.out
