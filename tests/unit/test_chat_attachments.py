from __future__ import annotations

import base64
import configparser
from pathlib import Path
from typing import Any

import pytest

from nyxgpt.chat import _build_user_message, chat, chat_stream

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _build_user_message unit tests
# ---------------------------------------------------------------------------


def test_build_user_message_no_attachments() -> None:
    msg = _build_user_message("hello", None)
    assert msg == {"role": "user", "content": "hello"}


def test_build_user_message_empty_attachments() -> None:
    msg = _build_user_message("hello", [])
    assert msg == {"role": "user", "content": "hello"}


def test_build_user_message_with_image() -> None:
    fake_b64 = base64.b64encode(b"fake-image-data").decode()
    attachments = [
        {"type": "image", "media_type": "image/jpeg", "data": fake_b64, "filename": "photo.jpg"}
    ]
    msg = _build_user_message("describe this", attachments)
    assert msg["role"] == "user"
    assert msg["content"] == "describe this"
    assert "images" in msg
    assert msg["images"] == [fake_b64]


def test_build_user_message_with_multiple_images() -> None:
    b1 = base64.b64encode(b"img1").decode()
    b2 = base64.b64encode(b"img2").decode()
    attachments = [
        {"type": "image", "media_type": "image/png", "data": b1, "filename": "a.png"},
        {"type": "image", "media_type": "image/webp", "data": b2, "filename": "b.webp"},
    ]
    msg = _build_user_message("two images", attachments)
    assert msg["images"] == [b1, b2]


def test_build_user_message_with_text_document() -> None:
    doc_text = "This is the document content."
    doc_b64 = base64.b64encode(doc_text.encode()).decode()
    attachments = [
        {"type": "document", "media_type": "text/plain", "data": doc_b64, "filename": "notes.txt"}
    ]
    msg = _build_user_message("summarize", attachments)
    assert msg["role"] == "user"
    assert "Attached document: notes.txt" in msg["content"]
    assert "This is the document content." in msg["content"]
    assert "summarize" in msg["content"]
    assert "images" not in msg


def test_build_user_message_document_text_prepended_before_prompt() -> None:
    doc_b64 = base64.b64encode(b"doc content").decode()
    attachments = [
        {"type": "document", "media_type": "text/plain", "data": doc_b64, "filename": "f.txt"}
    ]
    msg = _build_user_message("my prompt", attachments)
    # Document content should appear before the prompt
    doc_pos = msg["content"].find("doc content")
    prompt_pos = msg["content"].find("my prompt")
    assert doc_pos < prompt_pos


def test_build_user_message_mixed_image_and_document() -> None:
    img_b64 = base64.b64encode(b"imgdata").decode()
    doc_b64 = base64.b64encode(b"doc text").decode()
    attachments = [
        {"type": "image", "media_type": "image/jpeg", "data": img_b64, "filename": "img.jpg"},
        {"type": "document", "media_type": "text/plain", "data": doc_b64, "filename": "doc.txt"},
    ]
    msg = _build_user_message("analyze", attachments)
    assert "images" in msg
    assert msg["images"] == [img_b64]
    assert "doc text" in msg["content"]
    assert "analyze" in msg["content"]


def test_build_user_message_with_pdf_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF attachments should extract text via pdfplumber, not raw UTF-8 decode."""
    from unittest.mock import MagicMock, patch

    fake_pdf_bytes = b"%PDF-1.4 fake pdf binary content"
    pdf_b64 = base64.b64encode(fake_pdf_bytes).decode()
    extracted_text = "CHROMOSOME KARYOTYPE REPORT\nNormal female karyotype: 46,XX"

    mock_page = MagicMock()
    mock_page.extract_text.return_value = extracted_text
    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.pages = [mock_page]

    with patch("pdfplumber.open", return_value=mock_pdf):
        attachments = [
            {
                "type": "document",
                "media_type": "application/pdf",
                "data": pdf_b64,
                "filename": "report.pdf",
            }
        ]
        msg = _build_user_message("interpret this", attachments)

    assert "CHROMOSOME KARYOTYPE REPORT" in msg["content"]
    assert "46,XX" in msg["content"]
    assert "interpret this" in msg["content"]
    assert "Attached document: report.pdf" in msg["content"]
    assert "images" not in msg


def test_build_user_message_pdf_no_extractable_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """PDFs with no extractable text get a clear fallback message."""
    from unittest.mock import MagicMock, patch

    pdf_b64 = base64.b64encode(b"%PDF-1.4 scanned only").decode()

    mock_page = MagicMock()
    mock_page.extract_text.return_value = None
    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.pages = [mock_page]

    with patch("pdfplumber.open", return_value=mock_pdf):
        attachments = [
            {
                "type": "document",
                "media_type": "application/pdf",
                "data": pdf_b64,
                "filename": "scan.pdf",
            }
        ]
        msg = _build_user_message("what is this?", attachments)

    assert "no extractable text found" in msg["content"]
    assert "scan.pdf" in msg["content"]
    assert "what is this?" in msg["content"]


def test_build_user_message_document_bad_base64_skipped() -> None:
    attachments = [
        {
            "type": "document",
            "media_type": "text/plain",
            "data": "!!!notbase64!!!",
            "filename": "bad.txt",
        }
    ]
    # Should not raise; corrupted attachment is skipped gracefully
    msg = _build_user_message("prompt", attachments)
    assert msg["role"] == "user"
    # Prompt is still present even if doc decoding fails
    assert "prompt" in msg["content"]


# ---------------------------------------------------------------------------
# chat() integration tests with attachments
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["rag"] = {"enable_chat_context": "false"}
    cfg["cache"] = {
        "response_cache_enabled": "false",
        "embedding_cache_enabled": "false",
    }
    return cfg


def test_chat_with_image_attachment_passes_images_to_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    captured: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, Any]], **_: Any) -> str:
        captured["messages"] = messages
        return "image reply"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    img_b64 = base64.b64encode(b"fake-image").decode()
    attachments = [
        {"type": "image", "media_type": "image/jpeg", "data": img_b64, "filename": "test.jpg"}
    ]

    result = chat("describe this image", config_path=None, attachments=attachments)
    assert result.reply == "image reply"

    user_msg = next(m for m in captured["messages"] if m["role"] == "user")
    assert "images" in user_msg
    assert user_msg["images"] == [img_b64]


def test_chat_with_document_attachment_prepends_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    captured: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, Any]], **_: Any) -> str:
        captured["messages"] = messages
        return "doc reply"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    doc_text = "Important document content."
    doc_b64 = base64.b64encode(doc_text.encode()).decode()
    attachments = [
        {"type": "document", "media_type": "text/plain", "data": doc_b64, "filename": "report.txt"}
    ]

    result = chat("summarize this", config_path=None, attachments=attachments)
    assert result.reply == "doc reply"

    user_msg = next(m for m in captured["messages"] if m["role"] == "user")
    assert "Important document content." in user_msg["content"]
    assert "summarize this" in user_msg["content"]
    assert "images" not in user_msg


def test_chat_without_attachments_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    captured: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, Any]], **_: Any) -> str:
        captured["messages"] = messages
        return "normal reply"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("hello", config_path=None)
    assert result.reply == "normal reply"

    user_msg = next(m for m in captured["messages"] if m["role"] == "user")
    assert user_msg["content"] == "hello"
    assert "images" not in user_msg


def test_chat_stream_with_image_attachment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    captured: dict[str, Any] = {}

    def fake_stream_tokens(*, messages: list[dict[str, Any]], **_: Any):
        captured["messages"] = messages
        yield "streaming"
        yield " reply"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat_stream_tokens", fake_stream_tokens)
    monkeypatch.setattr("nyxgpt.chat.save_session", lambda *a, **k: None)

    img_b64 = base64.b64encode(b"fake-img").decode()
    attachments = [
        {"type": "image", "media_type": "image/png", "data": img_b64, "filename": "img.png"}
    ]

    chunks = list(chat_stream("look at this", config_path=None, attachments=attachments))
    assert "".join(chunks) == "streaming reply"

    user_msg = next(m for m in captured["messages"] if m["role"] == "user")
    assert "images" in user_msg
    assert user_msg["images"] == [img_b64]


# ---------------------------------------------------------------------------
# AttachmentBlock model tests
# ---------------------------------------------------------------------------


def test_attachment_block_model() -> None:
    from nyxgpt.api_models import AttachmentBlock

    block = AttachmentBlock(
        type="image", media_type="image/jpeg", data="abc123", filename="photo.jpg"
    )
    assert block.type == "image"
    assert block.media_type == "image/jpeg"
    assert block.data == "abc123"
    assert block.filename == "photo.jpg"


def test_attachment_block_optional_filename() -> None:
    from nyxgpt.api_models import AttachmentBlock

    block = AttachmentBlock(type="document", media_type="application/pdf", data="xyz")
    assert block.filename is None


def test_chat_request_accepts_attachments() -> None:
    from nyxgpt.api_models import AttachmentBlock, ChatRequest

    att = AttachmentBlock(type="image", media_type="image/png", data="b64data")
    req = ChatRequest(prompt="hello", attachments=[att])
    assert req.attachments is not None
    assert len(req.attachments) == 1
    assert req.attachments[0].type == "image"


def test_chat_request_without_attachments() -> None:
    from nyxgpt.api_models import ChatRequest

    req = ChatRequest(prompt="hello")
    assert req.attachments is None
