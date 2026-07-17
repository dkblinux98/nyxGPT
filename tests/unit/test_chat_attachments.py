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


def test_extract_document_text_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOCX attachments extract paragraph and table text."""
    from unittest.mock import MagicMock, patch

    from nyxgpt.chat import _extract_document_text

    fake_para = MagicMock()
    fake_para.text = "Report findings for patient."
    fake_cell = MagicMock()
    fake_cell.text = "46,XX"
    fake_row = MagicMock()
    fake_row.cells = [fake_cell]
    fake_table = MagicMock()
    fake_table.rows = [fake_row]
    mock_doc = MagicMock()
    mock_doc.paragraphs = [fake_para]
    mock_doc.tables = [fake_table]

    with patch("docx.Document", return_value=mock_doc):
        text = _extract_document_text(
            b"fake docx bytes",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "report.docx",
        )

    assert "Report findings" in text
    assert "46,XX" in text


def test_extract_document_text_pptx(monkeypatch: pytest.MonkeyPatch) -> None:
    """PPTX attachments extract slide text."""
    from unittest.mock import MagicMock, patch

    from nyxgpt.chat import _extract_document_text

    mock_shape = MagicMock()
    mock_shape.text = "Oncology Summary"
    mock_slide = MagicMock()
    mock_slide.shapes = [mock_shape]
    mock_prs = MagicMock()
    mock_prs.slides = [mock_slide]

    with patch("pptx.Presentation", return_value=mock_prs):
        text = _extract_document_text(
            b"fake pptx bytes",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "slides.pptx",
        )

    assert "[Slide 1]" in text
    assert "Oncology Summary" in text


def test_extract_document_text_epub(monkeypatch: pytest.MonkeyPatch) -> None:
    """ePUB attachments extract chapter text via BeautifulSoup."""
    from unittest.mock import MagicMock, patch

    from nyxgpt.chat import _extract_document_text

    mock_item = MagicMock()
    mock_item.get_content.return_value = b"<html><body><p>Chapter one text.</p></body></html>"
    mock_book = MagicMock()
    mock_book.get_items_of_type.return_value = [mock_item]

    with patch("ebooklib.epub.read_epub", return_value=mock_book):
        text = _extract_document_text(b"fake epub bytes", "application/epub+zip", "book.epub")

    assert "Chapter one text." in text


def test_extract_document_text_html() -> None:
    """HTML attachments strip tags and return readable text."""
    from nyxgpt.chat import _extract_document_text

    html = b"<html><body><script>alert(1)</script><p>Patient data.</p></body></html>"
    text = _extract_document_text(html, "text/html", "report.html")

    assert "Patient data." in text
    assert "alert" not in text


def test_extract_document_text_plain() -> None:
    """Plain text is returned as-is (UTF-8 decode)."""
    from nyxgpt.chat import _extract_document_text

    raw = b"Plain text content.\nSecond line."
    text = _extract_document_text(raw, "text/plain", "notes.txt")

    assert "Plain text content." in text
    assert "Second line." in text


def _install_fake_pdfplumber(monkeypatch: pytest.MonkeyPatch, mock_pdf: Any) -> None:
    """Install a fake ``pdfplumber`` module in ``sys.modules``.

    ``_extract_document_text`` does ``import pdfplumber`` lazily, so patching
    ``pdfplumber.open`` requires the real (optional, native-dependency-heavy)
    package to be importable. Stubbing the module directly keeps the test
    deterministic regardless of whether pdfplumber is installed in the
    running environment.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    fake_module = types.ModuleType("pdfplumber")
    fake_module.open = MagicMock(return_value=mock_pdf)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_module)


def test_build_user_message_with_pdf_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF attachments should extract text via pdfplumber, not raw UTF-8 decode."""
    from unittest.mock import MagicMock

    fake_pdf_bytes = b"%PDF-1.4 fake pdf binary content"
    pdf_b64 = base64.b64encode(fake_pdf_bytes).decode()
    extracted_text = "CHROMOSOME KARYOTYPE REPORT\nNormal female karyotype: 46,XX"

    mock_page = MagicMock()
    mock_page.extract_text.return_value = extracted_text
    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.pages = [mock_page]

    _install_fake_pdfplumber(monkeypatch, mock_pdf)

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
    from unittest.mock import MagicMock

    pdf_b64 = base64.b64encode(b"%PDF-1.4 scanned only").decode()

    mock_page = MagicMock()
    mock_page.extract_text.return_value = None
    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.pages = [mock_page]

    _install_fake_pdfplumber(monkeypatch, mock_pdf)

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
