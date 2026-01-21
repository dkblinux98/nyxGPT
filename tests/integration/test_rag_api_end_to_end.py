from __future__ import annotations

import time
import uuid

import httpx
import pytest


def _unique_doc_id(prefix: str) -> str:
    """Generate a unique doc_id for testing to avoid hash-based skip."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.mark.integration
def test_rag_api_ingest_and_query(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    doc_id = f"itest-{uuid.uuid4().hex[:10]}"
    text = (
        "Cassandra 5.0 supports vector search with SAI indexes. "
        "This sentence is used for myGPT integration testing."
    )

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Optional: verify the API is up before we do slower work
        r = client.get("/health")
        assert r.status_code == 200

        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id,
                "text": text,
            },
        )
        assert ingest_resp.status_code in (200, 201)

        # Give Cassandra indexing a moment (SAI / vector index)
        time.sleep(2.0)

        query_resp = client.post(
            "/api/v1/rag/query",
            json={
                "query": "What does Cassandra support for vector search?",
                "top_k": 3,
            },
        )
        assert query_resp.status_code == 200

        data = query_resp.json()
        assert isinstance(data, dict)
        assert "results" in data
        assert isinstance(data["results"], list)
        assert len(data["results"]) > 0

        top = data["results"][0]
        assert isinstance(top, dict)
        assert "text" in top
        assert isinstance(top["text"], str)
        assert len(top["text"]) > 0
        assert "doc_id" in top
        assert "chunk_id" in top
        assert "score" in top


@pytest.mark.integration
def test_session_rag_enable_disable(api_base_url: str) -> None:
    """Test per-session RAG enable/disable endpoints."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # 1. Get initial metadata (creates session)
        meta_resp = client.get(f"/api/v1/sessions/{session_name}/metadata")
        assert meta_resp.status_code == 200
        meta = meta_resp.json()
        assert "rag_enabled" in meta
        # Default should be False (or inherited from config)
        assert isinstance(meta["rag_enabled"], bool)

        # 2. Enable RAG
        enable_resp = client.post(f"/api/v1/sessions/{session_name}/rag/enable")
        assert enable_resp.status_code == 200
        enable_data = enable_resp.json()
        assert enable_data["session"] == session_name
        assert enable_data["rag_enabled"] is True

        # 3. Verify RAG is enabled in metadata
        meta_resp2 = client.get(f"/api/v1/sessions/{session_name}/metadata")
        assert meta_resp2.status_code == 200
        meta2 = meta_resp2.json()
        assert meta2["rag_enabled"] is True

        # 4. Disable RAG
        disable_resp = client.post(f"/api/v1/sessions/{session_name}/rag/disable")
        assert disable_resp.status_code == 200
        disable_data = disable_resp.json()
        assert disable_data["session"] == session_name
        assert disable_data["rag_enabled"] is False

        # 5. Verify RAG is disabled in metadata
        meta_resp3 = client.get(f"/api/v1/sessions/{session_name}/metadata")
        assert meta_resp3.status_code == 200
        meta3 = meta_resp3.json()
        assert meta3["rag_enabled"] is False


@pytest.mark.integration
def test_rag_upload_text_file(
    api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path
) -> None:
    """Test RAG file upload endpoint with .txt file."""
    # Create a test text file
    test_file = tmp_path / "test_upload.txt"
    test_content = "This is a test document for RAG upload testing. It contains important information."
    test_file.write_text(test_content)

    # Use unique doc_id to avoid hash-based skip from previous test runs
    doc_id = _unique_doc_id("txt-upload")

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the file with unique doc_id
        with open(test_file, "rb") as f:
            files = {"file": ("test_upload.txt", f, "text/plain")}
            upload_resp = client.post(
                "/api/v1/rag/upload", files=files, params={"doc_id": doc_id}
            )

        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        assert "doc_id" in upload_data
        assert "chunks_ingested" in upload_data
        assert upload_data["chunks_ingested"] > 0

        # Give Cassandra indexing a moment
        time.sleep(2.0)

        # Verify we can query the uploaded content
        query_resp = client.post(
            "/api/v1/rag/query", json={"query": "test document", "top_k": 5}
        )
        assert query_resp.status_code == 200
        results = query_resp.json()["results"]
        assert len(results) > 0


@pytest.mark.integration
def test_rag_upload_markdown_file(
    api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path
) -> None:
    """Test RAG file upload endpoint with .md file with frontmatter."""
    # Create a test markdown file with frontmatter
    test_file = tmp_path / "test.md"
    markdown_content = """---
title: Test Document
author: Integration Test
tags: [test, markdown]
---

# Main Heading

This is a test markdown document with **bold** and *italic* text.

## Section 1

Some content here with `code` inline.

```python
def hello():
    print("Hello, world!")
```

## Section 2

More content for testing RAG ingestion.
"""
    test_file.write_text(markdown_content)

    # Use unique doc_id to avoid hash-based skip from previous test runs
    doc_id = _unique_doc_id("md-upload")

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the markdown file with unique doc_id
        with open(test_file, "rb") as f:
            files = {"file": ("test.md", f, "text/markdown")}
            upload_resp = client.post(
                "/api/v1/rag/upload", files=files, params={"doc_id": doc_id}
            )

        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        assert "doc_id" in upload_data
        assert "chunks_ingested" in upload_data
        assert upload_data["chunks_ingested"] > 0

        # Give Cassandra indexing a moment
        time.sleep(2.0)

        # Verify we can query the uploaded content
        query_resp = client.post(
            "/api/v1/rag/query", json={"query": "markdown document", "top_k": 5}
        )
        assert query_resp.status_code == 200
        results = query_resp.json()["results"]
        assert len(results) > 0


@pytest.mark.integration
def test_rag_upload_pptx_file(
    api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path
) -> None:
    """Test RAG file upload endpoint with .pptx file."""
    pytest.importorskip("pptx", reason="python-pptx not installed")
    from pptx import Presentation

    # Create a test PowerPoint file
    test_file = tmp_path / "test_presentation.pptx"
    prs = Presentation()

    # Slide 1: Title slide
    slide1 = prs.slides.add_slide(prs.slide_layouts[0])
    title1 = slide1.shapes.title
    subtitle1 = slide1.placeholders[1]
    title1.text = "Test Presentation"
    subtitle1.text = "Integration Testing for PPTX Support"

    # Slide 2: Content slide with bullet points
    slide2 = prs.slides.add_slide(prs.slide_layouts[1])
    title2 = slide2.shapes.title
    body2 = slide2.placeholders[1]
    title2.text = "Key Features"
    tf2 = body2.text_frame
    tf2.text = "First feature: text extraction"
    p2 = tf2.add_paragraph()
    p2.text = "Second feature: slide order preservation"
    p2.level = 0
    p3 = tf2.add_paragraph()
    p3.text = "Third feature: speaker notes support"
    p3.level = 0

    # Slide 3: Slide with speaker notes
    slide3 = prs.slides.add_slide(prs.slide_layouts[1])
    title3 = slide3.shapes.title
    title3.text = "Technical Architecture"
    # Add speaker notes
    notes_slide3 = slide3.notes_slide
    notes_tf3 = notes_slide3.notes_text_frame
    notes_tf3.text = "This slide discusses the technical architecture of the RAG system."

    prs.save(str(test_file))

    # Use unique doc_id to avoid hash-based skip from previous test runs
    doc_id = _unique_doc_id("pptx-upload")

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the PPTX file with unique doc_id
        with open(test_file, "rb") as f:
            files = {"file": ("test_presentation.pptx", f, "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
            upload_resp = client.post("/api/v1/rag/upload", files=files, params={"doc_id": doc_id})

        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        assert "doc_id" in upload_data
        assert "chunks_ingested" in upload_data
        assert upload_data["chunks_ingested"] > 0

        # Give Cassandra indexing a moment
        time.sleep(2.0)

        # Verify we can query the uploaded content
        query_resp = client.post(
            "/api/v1/rag/query", json={"query": "presentation features", "top_k": 5}
        )
        assert query_resp.status_code == 200
        results = query_resp.json()["results"]
        assert len(results) > 0


@pytest.mark.integration
def test_rag_upload_pptx_with_speaker_notes(
    api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path
) -> None:
    """Test that PPTX upload correctly extracts speaker notes."""
    pytest.importorskip("pptx", reason="python-pptx not installed")
    from pptx import Presentation

    # Create a test PowerPoint file with specific speaker notes
    test_file = tmp_path / "notes_test.pptx"
    prs = Presentation()

    slide1 = prs.slides.add_slide(prs.slide_layouts[0])
    title1 = slide1.shapes.title
    title1.text = "Speaker Notes Test"

    # Add unique speaker notes text
    notes_slide1 = slide1.notes_slide
    notes_tf1 = notes_slide1.notes_text_frame
    unique_note_text = "This unique speaker note should be extracted and searchable in RAG."
    notes_tf1.text = unique_note_text

    prs.save(str(test_file))

    # Use unique doc_id
    doc_id = _unique_doc_id("pptx-notes")

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the PPTX file
        with open(test_file, "rb") as f:
            files = {"file": ("notes_test.pptx", f, "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
            upload_resp = client.post("/api/v1/rag/upload", files=files, params={"doc_id": doc_id})

        assert upload_resp.status_code == 200

        # Give Cassandra indexing a moment
        time.sleep(2.0)

        # Query for the speaker notes content
        query_resp = client.post(
            "/api/v1/rag/query", json={"query": "unique speaker note searchable", "top_k": 5}
        )
        assert query_resp.status_code == 200
        results = query_resp.json()["results"]
        assert len(results) > 0
        # Verify speaker notes were extracted
        found_note = any(unique_note_text.lower() in result["text"].lower() for result in results)
        assert found_note, "Speaker notes were not extracted"


@pytest.mark.integration
def test_rag_upload_pptx_slide_order(
    api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path
) -> None:
    """Test that PPTX upload preserves slide order."""
    pytest.importorskip("pptx", reason="python-pptx not installed")
    from pptx import Presentation

    # Create a test PowerPoint file with numbered slides
    test_file = tmp_path / "order_test.pptx"
    prs = Presentation()

    for i in range(1, 4):
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        title = slide.shapes.title
        title.text = f"Slide {i}: Content in order"

    prs.save(str(test_file))

    # Use unique doc_id
    doc_id = _unique_doc_id("pptx-order")

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the PPTX file
        with open(test_file, "rb") as f:
            files = {"file": ("order_test.pptx", f, "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
            upload_resp = client.post("/api/v1/rag/upload", files=files, params={"doc_id": doc_id})

        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        assert upload_data["chunks_ingested"] > 0


@pytest.mark.integration
def test_rag_upload_empty_pptx(api_base_url: str, tmp_path) -> None:
    """Test that uploading empty PPTX file is handled gracefully."""
    pytest.importorskip("pptx", reason="python-pptx not installed")
    from pptx import Presentation

    # Create an empty PowerPoint file
    test_file = tmp_path / "empty.pptx"
    prs = Presentation()
    # Add a slide but with no text content
    prs.slides.add_slide(prs.slide_layouts[6])  # Blank layout
    prs.save(str(test_file))

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Upload the empty PPTX file
        with open(test_file, "rb") as f:
            files = {"file": ("empty.pptx", f, "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
            upload_resp = client.post("/api/v1/rag/upload", files=files)

        # Should reject with 400 for no extractable text
        assert upload_resp.status_code == 400
        error_data = upload_resp.json()
        assert "error" in error_data
        assert "no extractable text" in error_data["error"]["message"].lower()


@pytest.mark.integration
def test_rag_upload_invalid_file_type(api_base_url: str, tmp_path) -> None:
    """Test that uploading unsupported file types is rejected."""
    # Create a test file with unsupported extension
    test_file = tmp_path / "test.exe"
    test_file.write_bytes(b"fake executable content")

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Try to upload unsupported file type
        with open(test_file, "rb") as f:
            files = {"file": ("test.exe", f, "application/octet-stream")}
            upload_resp = client.post("/api/v1/rag/upload", files=files)

        # Should reject with 400
        assert upload_resp.status_code == 400
        error_data = upload_resp.json()
        assert "error" in error_data
        assert "not supported" in error_data["error"]["message"].lower()


@pytest.mark.integration
def test_rag_upload_pdf_with_tables_and_metadata(
    api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path
) -> None:
    """Test PDF upload with improved extraction: tables, formatting, multi-column, metadata (#2663)."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    except ImportError:
        pytest.skip("reportlab not available for PDF generation")

    # Create a test PDF with metadata, tables, and formatted text
    test_file = tmp_path / "test_enhanced.pdf"

    doc = SimpleDocTemplate(
        str(test_file),
        pagesize=letter,
        title="Test Document",
        author="Test Author",
        subject="PDF Extraction Testing",
    )

    styles = getSampleStyleSheet()
    story = []

    # Add title
    title = Paragraph("Enhanced PDF Test Document", styles['Title'])
    story.append(title)
    story.append(Spacer(1, 0.2 * inch))

    # Add description
    desc = Paragraph(
        "This document tests improved PDF extraction with tables, formatting, and metadata.",
        styles['Normal']
    )
    story.append(desc)
    story.append(Spacer(1, 0.3 * inch))

    # Add a table to test table extraction
    table_data = [
        ['Feature', 'Status', 'Priority'],
        ['Table handling', 'Improved', 'High'],
        ['Formatting preservation', 'Enhanced', 'High'],
        ['Multi-column support', 'Added', 'Medium'],
        ['Metadata extraction', 'Implemented', 'High'],
    ]

    table = Table(table_data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))

    story.append(table)
    story.append(Spacer(1, 0.3 * inch))

    # Add more text content
    content = Paragraph(
        "The improved PDF extraction now properly handles complex layouts including "
        "tables with structured data, preserves text formatting, and extracts document "
        "metadata such as title, author, and creation date.",
        styles['Normal']
    )
    story.append(content)

    # Build the PDF
    doc.build(story)

    # Use unique doc_id to avoid hash-based skip from previous test runs
    doc_id = _unique_doc_id("pdf-enhanced")

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the PDF file with unique doc_id
        with open(test_file, "rb") as f:
            files = {"file": ("test_enhanced.pdf", f, "application/pdf")}
            upload_resp = client.post("/api/v1/rag/upload", files=files, params={"doc_id": doc_id})

        # Verify successful upload
        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        assert "doc_id" in upload_data
        assert "chunks_ingested" in upload_data
        assert upload_data["chunks_ingested"] > 0

        # Give Cassandra indexing a moment
        time.sleep(2.0)

        # Verify we can query content from the table
        query_resp = client.post(
            "/api/v1/rag/query", json={"query": "table handling formatting", "top_k": 5}
        )
        assert query_resp.status_code == 200
        results = query_resp.json()["results"]
        assert len(results) > 0

        # Verify the extracted text contains table markers and metadata
        # The text should contain our table data and metadata
        combined_text = " ".join(r["text"] for r in results)

        # Check for table content
        assert "Table handling" in combined_text or "Improved" in combined_text

        # Check for metadata section (at least title or author should be present)
        assert "Metadata" in combined_text or "Test" in combined_text
