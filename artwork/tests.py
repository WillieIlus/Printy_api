from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from artwork.services.pdf_analysis import analyze_pdf


class _Rect:
    def __init__(self, width_pts: float, height_pts: float):
        self.width = width_pts
        self.height = height_pts


class _Pixmap:
    def tobytes(self, image_format: str) -> bytes:
        return b"preview-bytes"


class _Page:
    def __init__(self, width_mm: float, height_mm: float):
        self.rect = _Rect(width_mm * 72 / 25.4, height_mm * 72 / 25.4)

    def get_pixmap(self, matrix=None, alpha=False):
        return _Pixmap()


class _Doc:
    def __init__(self, pages_mm: list[tuple[float, float]]):
        self._pages = [_Page(width_mm, height_mm) for width_mm, height_mm in pages_mm]
        self.page_count = len(self._pages)

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, index: int):
        return self._pages[index]

    def __iter__(self):
        return iter(self._pages)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeFileDataError(Exception):
    pass


class PdfAnalysisSuggestionTests(SimpleTestCase):
    def test_business_card_pdf_suggests_business_cards(self):
        result = self._analyze_pdf([(85, 55), (85, 55)])

        self.assertEqual(result["suggested_product"]["type"], "business_card")
        self.assertEqual(result["suggested_product"]["confidence"], "high")
        self.assertEqual(result["detected"]["size_label"], "Business Card")
        self.assertIn(
            {
                "field": "product_type",
                "value": "business_card",
                "label": "Business Cards",
                "confidence": "high",
            },
            result["suggestions"],
        )
        self.assertIn(
            {
                "field": "finished_size",
                "value": "85x55mm",
                "label": "Standard Business Card",
                "confidence": "high",
            },
            result["suggestions"],
        )

    def test_booklet_warns_when_pages_are_not_multiple_of_four(self):
        result = self._analyze_pdf([(210, 297)] * 10)

        self.assertEqual(result["suggested_product"]["type"], "booklet")
        self.assertEqual(result["suggested_product"]["confidence"], "medium")
        self.assertIn("Pages may need to be rounded to a multiple of 4 for saddle stitch.", result["warnings"])
        self.assertIn(
            {
                "field": "total_pages",
                "value": 10,
                "label": "10 pages",
                "confidence": "medium",
            },
            result["suggestions"],
        )

    def test_booklet_high_confidence_for_multiple_of_four(self):
        result = self._analyze_pdf([(210, 297)] * 48)

        self.assertEqual(result["suggested_product"]["type"], "booklet")
        self.assertEqual(result["suggested_product"]["confidence"], "high")
        self.assertNotIn("Pages may need to be rounded to a multiple of 4 for saddle stitch.", result["warnings"])

    def test_large_format_pdf_suggests_poster_without_calculator_product(self):
        result = self._analyze_pdf([(600, 900)])

        self.assertEqual(result["suggested_product"]["type"], "poster_large_format")
        self.assertEqual(result["suggestions"], [])

    def test_uploaded_file_source_reads_pdf_without_file_path(self):
        fake_fitz = self._fake_fitz([(210, 297)])
        upload = SimpleUploadedFile("sample.pdf", b"%PDF-1.7 fake bytes", content_type="application/pdf")

        with patch.dict(sys.modules, {"fitz": fake_fitz}):
            result = analyze_pdf(upload)

        self.assertEqual(result["analysis_status"], "analysed")
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["width_mm"], 210.0)
        self.assertEqual(result["height_mm"], 297.0)
        self.assertEqual(result["detected"]["unit"], "mm")
        self.assertEqual(result["detected"]["points_to_mm"], round(25.4 / 72, 6))

    def test_corrupt_pdf_returns_structured_unreadable_status(self):
        fake_fitz = self._fake_fitz_error(_FakeFileDataError("cannot open broken document"))

        with patch.dict(sys.modules, {"fitz": fake_fitz}):
            result = analyze_pdf("broken.pdf")

        self.assertEqual(result["analysis_status"], "unreadable")
        self.assertEqual(result["analysis_error_code"], "corrupt_or_unreadable_pdf")
        self.assertEqual(result["analysis_error"], "PDF is unreadable or corrupt")

    def _analyze_pdf(self, pages_mm: list[tuple[float, float]]) -> dict:
        fake_fitz = self._fake_fitz(pages_mm)
        with patch.dict(sys.modules, {"fitz": fake_fitz}):
            return analyze_pdf("dummy.pdf")

    def _fake_fitz(self, pages_mm: list[tuple[float, float]]):
        def open_pdf(path=None, stream=None, filetype=None):
            return _Doc(pages_mm)

        return SimpleNamespace(
            open=open_pdf,
            Matrix=lambda x, y: (x, y),
            FileDataError=_FakeFileDataError,
        )

    def _fake_fitz_error(self, exc: Exception):
        def open_pdf(path=None, stream=None, filetype=None):
            raise exc

        return SimpleNamespace(
            open=open_pdf,
            Matrix=lambda x, y: (x, y),
            FileDataError=_FakeFileDataError,
        )
