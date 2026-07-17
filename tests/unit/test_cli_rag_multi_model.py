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


def test_rag_wipe_without_confirmation_flag_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that rag wipe refuses to run without --yes-really."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        exit_code = cli(["rag", "wipe"])

        assert exit_code == 2
        MockStore.assert_not_called()

        captured = capsys.readouterr()
        assert "refusing to wipe RAG store without --yes-really" in captured.err


def test_rag_ingest_skipped_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag ingest reports a skip when the document hash is unchanged."""
    from nyxgpt.cli import cli

    test_file = tmp_path / "test.txt"
    test_file.write_text("Unchanged content.")

    with patch("nyxgpt.cli.ingest_document") as mock_ingest:
        mock_ingest.return_value = {
            "status": "skipped",
            "chunks_ingested": 0,
            "doc_hash": "abcdef0123456789abcdef0123456789",
            "previous_hash": "abcdef0123456789abcdef0123456789",
        }

        exit_code = cli(["rag", "ingest", "unchanged-doc", str(test_file)])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "unchanged" in captured.out
        assert "skipped re-ingestion" in captured.out


def test_rag_ingest_updated_status_with_previous_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test rag ingest reports an update, including the previous document hash."""
    from nyxgpt.cli import cli

    test_file = tmp_path / "test.txt"
    test_file.write_text("Changed content.")

    with patch("nyxgpt.cli.ingest_document") as mock_ingest:
        mock_ingest.return_value = {
            "status": "updated",
            "chunks_ingested": 3,
            "doc_hash": "newhash0123456789newhash0123456789",
            "previous_hash": "oldhash0123456789oldhash0123456789",
        }

        exit_code = cli(["rag", "ingest", "changed-doc", str(test_file)])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Updated 3 chunks" in captured.out
        assert "Document hash: newhash012345678" in captured.out
        assert "Previous hash: oldhash012345678" in captured.out


def test_rag_query_with_metadata_filters(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag query builds and prints metadata filters, and result doc details."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.retrieve_context") as mock_retrieve:
        mock_retrieve.return_value = [
            {
                "text": "Result 1",
                "score": 0.9,
                "doc_id": "doc-a",
                "chunk_id": "chunk-1",
                "metadata": {"filename": "a.py", "tags": ["python", "core"]},
            }
        ]

        exit_code = cli(
            [
                "rag",
                "query",
                "test question",
                "--doc-ids",
                "doc-a,doc-b",
                "--filename",
                "a.py",
                "--tags",
                "python,core",
                "--date-from",
                "2024-01-01",
                "--date-to",
                "2024-12-31",
            ]
        )

        assert exit_code == 0
        mock_retrieve.assert_called_once()
        args, kwargs = mock_retrieve.call_args
        metadata_filter = kwargs["metadata_filter"]
        assert metadata_filter.doc_ids == ["doc-a", "doc-b"]
        assert metadata_filter.filename == "a.py"
        assert metadata_filter.tags == ["python", "core"]

        captured = capsys.readouterr()
        assert "Applied metadata filters:" in captured.out
        assert "doc_ids: doc-a,doc-b" in captured.out
        assert "filename: a.py" in captured.out
        assert "tags: python,core" in captured.out
        assert "date_from: 2024-01-01" in captured.out
        assert "date_to: 2024-12-31" in captured.out
        assert "[doc_id: doc-a, chunk_id: chunk-1]" in captured.out
        assert "[filename: a.py]" in captured.out
        assert "[tags: python, core]" in captured.out


def test_rag_info_found(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag info prints document version metadata when found."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        mock_store.get_document_info.return_value = {
            "doc_id": "doc-a",
            "chunks": 4,
            "embedding_model": "nomic-embed-text",
            "doc_hash": "abc123",
            "ingested_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-02T00:00:00",
        }
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "info", "doc-a", "--collection", "all-minilm"])

        assert exit_code == 0
        MockStore.assert_called_once_with(collection="all-minilm")
        mock_store.close.assert_called_once()

        captured = capsys.readouterr()
        assert "Document: doc-a" in captured.out
        assert "Chunks: 4" in captured.out
        assert "Embedding model: nomic-embed-text" in captured.out


def test_rag_info_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag info reports missing documents with exit code 1."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        mock_store.get_document_info.return_value = None
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "info", "missing-doc"])

        assert exit_code == 1
        mock_store.close.assert_called_once()

        captured = capsys.readouterr()
        assert "not found in collection" in captured.out


def test_rag_list_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag list with no documents in the collection."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        mock_store.list_docs.return_value = []
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "list"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "No documents found in collection" in captured.out


def test_rag_collections_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag collections with no collections available."""
    from nyxgpt.cli import cli

    with patch("nyxgpt.cli.CassandraVectorStore") as MockStore:
        mock_store = Mock()
        mock_store.list_collections.return_value = []
        MockStore.return_value = mock_store

        exit_code = cli(["rag", "collections"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "No collections found" in captured.out


def test_rag_compare_invalid_dimension(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag compare with a non-integer dimension in the model spec."""
    from nyxgpt.cli import cli

    test_file = tmp_path / "test.txt"
    test_file.write_text("Some test content.")

    exit_code = cli(["rag", "compare", str(test_file), "nomic-embed-text:not-a-number:default"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Invalid dimension 'not-a-number'" in captured.err


def test_rag_compare_file_read_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag compare when the test file can't be read."""
    from nyxgpt.cli import cli

    exit_code = cli(
        [
            "rag",
            "compare",
            "/nonexistent/path/does-not-exist.txt",
            "nomic-embed-text:768:default",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Failed to read test file" in captured.err


def test_rag_compare_no_usable_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag compare when the test file contains no usable sentences."""
    from nyxgpt.cli import cli

    test_file = tmp_path / "empty.txt"
    test_file.write_text("   ")

    exit_code = cli(["rag", "compare", str(test_file), "nomic-embed-text:768:default"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Test file contains no usable text" in captured.err


def test_rag_compare_exception_during_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test rag compare surfaces exceptions raised by compare_models."""
    from nyxgpt.cli import cli

    test_file = tmp_path / "test.txt"
    test_file.write_text("This is a test document. It has content.")

    with patch("nyxgpt.rag.model_compare.compare_models") as mock_compare:
        mock_compare.side_effect = RuntimeError("embedding service unreachable")

        exit_code = cli(["rag", "compare", str(test_file), "nomic-embed-text:768:default"])

        assert exit_code == 2
        captured = capsys.readouterr()
        assert "Comparison failed: embedding service unreachable" in captured.err


def test_rag_index_repo_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag index-repo reports files/chunks indexed on success."""
    from nyxgpt.cli import cli

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with patch("nyxgpt.rag.rag.ingest_repository") as mock_ingest_repo:
        mock_ingest_repo.return_value = {"total_files": 12, "total_chunks": 48}

        exit_code = cli(
            [
                "rag",
                "index-repo",
                str(repo_dir),
                "--prefix",
                "myrepo",
                "--extensions",
                "py,js",
                "--docs-only",
                "--collection",
                "code-collection",
                "--model",
                "nomic-embed-text",
                "--dimension",
                "768",
            ]
        )

        assert exit_code == 0
        mock_ingest_repo.assert_called_once()
        _, kwargs = mock_ingest_repo.call_args
        assert kwargs["doc_id_prefix"] == "myrepo"
        assert kwargs["extensions"] == {".py", ".js"}
        assert kwargs["extract_docs_only"] is True
        assert kwargs["collection"] == "code-collection"

        captured = capsys.readouterr()
        assert "Files indexed: 12" in captured.out
        assert "Total chunks: 48" in captured.out
        assert "Mode: Documentation only" in captured.out
        assert "Using embedding model: nomic-embed-text" in captured.out


def test_rag_index_repo_full_code_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Without --docs-only, rag index-repo reports "Mode: Full code"."""
    from nyxgpt.cli import cli

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with patch("nyxgpt.rag.rag.ingest_repository") as mock_ingest_repo:
        mock_ingest_repo.return_value = {"total_files": 3, "total_chunks": 9}

        exit_code = cli(["rag", "index-repo", str(repo_dir)])

        assert exit_code == 0
        _, kwargs = mock_ingest_repo.call_args
        assert kwargs["extract_docs_only"] is False

        captured = capsys.readouterr()
        assert "Mode: Full code" in captured.out


def test_rag_index_repo_exception(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test rag index-repo reports a failure when ingestion raises."""
    from nyxgpt.cli import cli

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with patch("nyxgpt.rag.rag.ingest_repository") as mock_ingest_repo:
        mock_ingest_repo.side_effect = ValueError("repository path escapes allowed roots")

        exit_code = cli(["rag", "index-repo", str(repo_dir)])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Failed to index repository" in captured.err
