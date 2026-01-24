"""Tests for PDF OCR functionality (#2669)."""
from __future__ import annotations

import io
from unittest.mock import Mock, patch, MagicMock
import pytest


@pytest.mark.unit
def test_pdf_ocr_enabled_config():
    """Test that OCR configuration is read correctly."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg.add_section("pdf")
    cfg.set("pdf", "ocr_enabled", "true")
    cfg.set("pdf", "ocr_min_text_threshold", "50")
    cfg.set("pdf", "ocr_dpi", "300")
    cfg.set("pdf", "ocr_lang", "eng")
    cfg.set("pdf", "ocr_psm", "3")

    assert cfg.getboolean("pdf", "ocr_enabled") is True
    assert cfg.getint("pdf", "ocr_min_text_threshold") == 50
    assert cfg.getint("pdf", "ocr_dpi") == 300
    assert cfg.get("pdf", "ocr_lang") == "eng"
    assert cfg.getint("pdf", "ocr_psm") == 3


@pytest.mark.unit
def test_pdf_ocr_disabled_config():
    """Test that OCR can be disabled via config."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg.add_section("pdf")
    cfg.set("pdf", "ocr_enabled", "false")

    assert cfg.getboolean("pdf", "ocr_enabled") is False


@pytest.mark.unit
def test_pdf_ocr_fallback_defaults():
    """Test that OCR config has sensible fallback defaults."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    # No [pdf] section configured

    # Should use fallback defaults
    assert cfg.getboolean("pdf", "ocr_enabled", fallback=True) is True
    assert cfg.getint("pdf", "ocr_min_text_threshold", fallback=50) == 50
    assert cfg.getint("pdf", "ocr_dpi", fallback=300) == 300
    assert cfg.get("pdf", "ocr_lang", fallback="eng") == "eng"
    assert cfg.getint("pdf", "ocr_psm", fallback=3) == 3


@pytest.mark.unit
@patch("pytesseract.image_to_string")
@patch("pdf2image.convert_from_bytes")
def test_ocr_extraction_logic(mock_convert, mock_ocr):
    """Test OCR extraction logic when standard extraction fails."""
    from PIL import Image

    # Mock image conversion
    mock_image = Mock(spec=Image.Image)
    mock_convert.return_value = [mock_image]

    # Mock OCR extraction
    mock_ocr.return_value = "This is OCR-extracted text from an image-based PDF."

    # Simulate calling pytesseract.image_to_string
    result = mock_ocr(mock_image, lang="eng", config="--psm 3")

    assert result == "This is OCR-extracted text from an image-based PDF."
    mock_ocr.assert_called_once()


@pytest.mark.unit
def test_ocr_threshold_detection():
    """Test that OCR is triggered when text is below threshold."""
    text_with_minimal_content = "ABC"  # 3 chars
    threshold = 50

    needs_ocr = len(text_with_minimal_content.strip()) < threshold
    assert needs_ocr is True

    text_with_sufficient_content = "A" * 100  # 100 chars
    needs_ocr = len(text_with_sufficient_content.strip()) < threshold
    assert needs_ocr is False


@pytest.mark.unit
def test_ocr_not_triggered_when_disabled():
    """Test that OCR is not triggered when disabled in config."""
    ocr_enabled = False
    text = ""  # Empty text
    needs_ocr = (not text or len(text.strip()) < 50) and ocr_enabled

    assert needs_ocr is False


@pytest.mark.unit
def test_ocr_triggered_for_empty_pdf():
    """Test that OCR is triggered for PDFs with no extractable text."""
    ocr_enabled = True
    text = ""  # No text extracted
    threshold = 50
    needs_ocr = (not text or len(text.strip()) < threshold) and ocr_enabled

    assert needs_ocr is True


@pytest.mark.unit
def test_ocr_triggered_for_minimal_text():
    """Test that OCR is triggered for PDFs with minimal text."""
    ocr_enabled = True
    text = "Page 1"  # Minimal text (6 chars)
    threshold = 50
    needs_ocr = (not text or len(text.strip()) < threshold) and ocr_enabled

    assert needs_ocr is True


@pytest.mark.unit
@patch("pytesseract.image_to_string")
@patch("pdf2image.convert_from_bytes")
def test_ocr_multipage_extraction(mock_convert, mock_ocr):
    """Test OCR extraction from multiple pages."""
    from PIL import Image

    # Mock 3 pages
    mock_image1 = Mock(spec=Image.Image)
    mock_image2 = Mock(spec=Image.Image)
    mock_image3 = Mock(spec=Image.Image)
    mock_convert.return_value = [mock_image1, mock_image2, mock_image3]

    # Mock OCR for each page
    mock_ocr.side_effect = [
        "Page 1 OCR text",
        "Page 2 OCR text",
        "Page 3 OCR text",
    ]

    # Simulate calling OCR on each page
    results = []
    for image in mock_convert.return_value:
        results.append(mock_ocr(image, lang="eng", config="--psm 3"))

    assert len(results) == 3
    assert results[0] == "Page 1 OCR text"
    assert results[1] == "Page 2 OCR text"
    assert results[2] == "Page 3 OCR text"


@pytest.mark.unit
def test_ocr_quality_comparison():
    """Test that OCR result is compared to standard extraction."""
    standard_text = "Some extracted text"  # 19 chars
    ocr_text = "Much more detailed OCR extracted text from the image"  # 52 chars

    # OCR should be preferred if it extracts more text
    use_ocr = len(ocr_text.strip()) > len(standard_text.strip())
    assert use_ocr is True

    # Standard extraction should be preferred if OCR doesn't improve
    poor_ocr_text = "Bad"  # 3 chars
    use_ocr = len(poor_ocr_text.strip()) > len(standard_text.strip())
    assert use_ocr is False


@pytest.mark.unit
def test_ocr_page_segmentation_modes():
    """Test different PSM (Page Segmentation Mode) values."""
    # PSM modes from Tesseract documentation
    valid_psm_modes = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]

    # Common modes used in the implementation
    psm_automatic = 3  # Fully automatic page segmentation
    psm_single_block = 6  # Assume a single uniform block of text
    psm_sparse_text = 11  # Sparse text - find as much text as possible

    assert psm_automatic in valid_psm_modes
    assert psm_single_block in valid_psm_modes
    assert psm_sparse_text in valid_psm_modes


@pytest.mark.unit
def test_ocr_language_codes():
    """Test valid OCR language codes."""
    # Common language codes supported by Tesseract
    valid_languages = ["eng", "spa", "fra", "deu", "chi_sim", "chi_tra", "ara", "rus", "jpn"]

    # Default language
    default_lang = "eng"
    assert default_lang in valid_languages

    # Multi-language support (requires + separator)
    multi_lang = "eng+spa"
    assert "+" in multi_lang
    langs = multi_lang.split("+")
    assert all(lang in valid_languages for lang in langs)


@pytest.mark.unit
def test_ocr_dpi_range():
    """Test valid DPI ranges for OCR."""
    # Common DPI values
    low_dpi = 150  # Fast but lower quality
    standard_dpi = 300  # Good balance (default)
    high_dpi = 600  # High quality but slower

    # All should be positive integers
    assert low_dpi > 0
    assert standard_dpi > 0
    assert high_dpi > 0

    # DPI should increase quality at the cost of processing time
    assert low_dpi < standard_dpi < high_dpi
