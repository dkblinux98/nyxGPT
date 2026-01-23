"""Tests for RAG lazy loading feature."""

from pathlib import Path
from nyxgpt import sessions


def test_rag_chunks_persisted_with_message(tmp_path: Path):
    """Test that RAG chunks are saved with messages."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Create a session with a message that has RAG chunks
    session_name = "test-rag-session"
    sf = sessions.session_file_for(session_name, sessions_dir)

    test_messages = [
        {
            "role": "user",
            "content": "What is Python?",
            "id": "msg1",
            "timestamp": "2024-01-01T00:00:00Z",
        },
        {
            "role": "assistant",
            "content": "Python is a programming language.",
            "id": "msg2",
            "timestamp": "2024-01-01T00:00:01Z",
            "rag_chunks": [
                {
                    "text": "Python is a high-level programming language.",
                    "score": 0.95,
                    "doc_id": "doc_python_intro",
                    "chunk_id": 1,
                },
                {
                    "text": "Python was created by Guido van Rossum.",
                    "score": 0.87,
                    "doc_id": "doc_python_history",
                    "chunk_id": 5,
                },
            ],
        },
    ]

    sessions.save_session_messages(sf, test_messages)

    # Load the messages back
    loaded_messages = sessions.load_session_messages(sf)

    assert len(loaded_messages) == 2
    assert loaded_messages[1]["role"] == "assistant"
    assert "rag_chunks" in loaded_messages[1]
    assert len(loaded_messages[1]["rag_chunks"]) == 2
    assert (
        loaded_messages[1]["rag_chunks"][0]["text"]
        == "Python is a high-level programming language."
    )
    assert loaded_messages[1]["rag_chunks"][0]["score"] == 0.95
    assert loaded_messages[1]["rag_chunks"][1]["doc_id"] == "doc_python_history"
    assert loaded_messages[1]["rag_chunks"][1]["chunk_id"] == 5


def test_messages_without_rag_chunks_still_work(tmp_path: Path):
    """Test that messages without RAG chunks continue to work normally."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_name = "test-normal-session"
    sf = sessions.session_file_for(session_name, sessions_dir)

    test_messages = [
        {
            "role": "user",
            "content": "Hello",
            "id": "msg1",
            "timestamp": "2024-01-01T00:00:00Z",
        },
        {
            "role": "assistant",
            "content": "Hi there!",
            "id": "msg2",
            "timestamp": "2024-01-01T00:00:01Z",
        },
    ]

    sessions.save_session_messages(sf, test_messages)
    loaded_messages = sessions.load_session_messages(sf)

    assert len(loaded_messages) == 2
    assert "rag_chunks" not in loaded_messages[1]


def test_rag_chunks_optional_fields(tmp_path: Path):
    """Test that RAG chunks work with optional doc_id and chunk_id."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_name = "test-optional-fields"
    sf = sessions.session_file_for(session_name, sessions_dir)

    test_messages = [
        {
            "role": "user",
            "content": "Test",
            "id": "msg1",
            "timestamp": "2024-01-01T00:00:00Z",
        },
        {
            "role": "assistant",
            "content": "Response",
            "id": "msg2",
            "timestamp": "2024-01-01T00:00:01Z",
            "rag_chunks": [
                {
                    "text": "Some context",
                    "score": 0.8,
                    "doc_id": None,
                    "chunk_id": None,
                }
            ],
        },
    ]

    sessions.save_session_messages(sf, test_messages)
    loaded_messages = sessions.load_session_messages(sf)

    assert len(loaded_messages[1]["rag_chunks"]) == 1
    assert loaded_messages[1]["rag_chunks"][0]["doc_id"] is None
    assert loaded_messages[1]["rag_chunks"][0]["chunk_id"] is None
