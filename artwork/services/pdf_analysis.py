from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger("artwork.pdf")

POINTS_TO_MM = 25.4 / 72

STANDARD_SIZES = {
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "A6": (105, 148),
    "DL": (99, 210),
    "Business Card": (85, 55),
}

SMALL_ITEM_TOLERANCE_MM = 5
LARGE_ITEM_TOLERANCE_MM = 10
SADDLE_STITCH_WARNING = "Pages may need to be rounded to a multiple of 4 for saddle stitch."

SIZE_CANDIDATES = [
    {"label": "Business Card", "value": "85x55mm", "display_label": "Standard Business Card", "width_mm": 85, "height_mm": 55, "tolerance_mm": SMALL_ITEM_TOLERANCE_MM},
    {"label": "Business Card", "value": "90x55mm", "display_label": "Standard Business Card", "width_mm": 90, "height_mm": 55, "tolerance_mm": SMALL_ITEM_TOLERANCE_MM},
    {"label": "Business Card", "value": None, "display_label": "Slim Business Card", "width_mm": 89, "height_mm": 51, "tolerance_mm": SMALL_ITEM_TOLERANCE_MM},
    {"label": "DL", "value": None, "display_label": "DL", "width_mm": 99, "height_mm": 210, "tolerance_mm": LARGE_ITEM_TOLERANCE_MM},
    {"label": "A5", "value": "A5", "display_label": "A5", "width_mm": 148, "height_mm": 210, "tolerance_mm": LARGE_ITEM_TOLERANCE_MM},
    {"label": "A4", "value": "A4", "display_label": "A4", "width_mm": 210, "height_mm": 297, "tolerance_mm": LARGE_ITEM_TOLERANCE_MM},
    {"label": "A3", "value": "A3", "display_label": "A3", "width_mm": 297, "height_mm": 420, "tolerance_mm": LARGE_ITEM_TOLERANCE_MM},
]


def analyze_pdf(source: Any) -> dict[str, Any]:
    source_name = _source_name(source)

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        logger.exception("PDF analysis dependency missing for %s", source_name)
        return _failed_result(
            status="dependency_missing",
            error_code="dependency_missing",
            error="PDF analysis dependency missing",
            warnings=["We could not read PDF details automatically."],
            confidence="none",
            technical_detail=str(exc),
        )

    try:
        with _open_document(fitz, source) as doc:
            if doc.page_count == 0:
                return _failed_result(
                    status="unreadable",
                    error_code="pdf_has_no_pages",
                    error="PDF has no pages",
                    warnings=["We could not read PDF details automatically."],
                )

            first_page = doc[0]
            width_mm = round(first_page.rect.width * POINTS_TO_MM, 1)
            height_mm = round(first_page.rect.height * POINTS_TO_MM, 1)

            sizes = {
                (round(page.rect.width, 1), round(page.rect.height, 1))
                for page in doc
            }
            warnings: list[str] = []
            if len(sizes) > 1:
                warnings.append("Mixed page sizes detected")

            preview_bytes = _render_preview(first_page)
            size_match = _match_size(width_mm, height_mm)
            size_label = size_match["label"] if size_match else None
            suggested_product = _suggest_product(
                pages=doc.page_count,
                width_mm=width_mm,
                height_mm=height_mm,
                size_match=size_match,
                warnings=warnings,
            )
            suggestions = _build_suggestions(
                pages=doc.page_count,
                suggested_product=suggested_product,
                size_match=size_match,
            )

            return {
                "analysis_status": "analysed",
                "analysis_error": None,
                "analysis_error_code": None,
                "analysis_technical_detail": None,
                "detected": {
                    "pages": doc.page_count,
                    "width_mm": width_mm,
                    "height_mm": height_mm,
                    "size_label": size_label,
                    "unit": "mm",
                    "points_to_mm": round(POINTS_TO_MM, 6),
                },
                "pages": doc.page_count,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "preview_format": "jpeg" if preview_bytes else None,
                "_preview_bytes": preview_bytes,
                "suggested_product": suggested_product,
                "suggestions": suggestions,
                "confidence": suggested_product["confidence"] if suggested_product else ("high" if not warnings else "medium"),
                "warnings": warnings,
                "analysis_warnings": warnings,
                "size_label": size_label,
            }
    except Exception as exc:
        status_value, error_code, error_message = _classify_exception(exc)
        logger.exception(
            "PDF analysis failed (%s/%s) for %s",
            status_value,
            error_code,
            source_name,
        )
        return _failed_result(
            status=status_value,
            error_code=error_code,
            error=error_message,
            warnings=["We could not read PDF details automatically."],
            technical_detail=str(exc),
        )


def _open_document(fitz_module: Any, source: Any) -> Any:
    if isinstance(source, (str, Path)):
        return fitz_module.open(str(source))

    file_obj = source
    if hasattr(file_obj, "open"):
        file_obj.open("rb")
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    pdf_bytes = file_obj.read()
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    if not pdf_bytes:
        raise ValueError("Uploaded file is empty.")

    return fitz_module.open(stream=pdf_bytes, filetype="pdf")


def _render_preview(page: Any) -> bytes | None:
    try:
        pix = page.get_pixmap(matrix=_fitz_matrix(0.35, 0.35), alpha=False)
        return pix.tobytes("jpeg")
    except Exception:
        logger.warning("PDF preview generation failed.", exc_info=True)
        return None


def _fitz_matrix(scale_x: float, scale_y: float) -> Any:
    import fitz  # PyMuPDF

    return fitz.Matrix(scale_x, scale_y)


def _failed_result(
    *,
    status: str,
    error_code: str,
    error: str,
    warnings: list[str],
    confidence: str = "low",
    technical_detail: str | None = None,
) -> dict[str, Any]:
    return {
        "analysis_status": status,
        "analysis_error": error,
        "analysis_error_code": error_code,
        "analysis_technical_detail": technical_detail,
        "detected": None,
        "pages": None,
        "width_mm": None,
        "height_mm": None,
        "preview_format": None,
        "_preview_bytes": None,
        "suggested_product": None,
        "suggestions": [],
        "confidence": confidence,
        "warnings": warnings,
        "analysis_warnings": warnings,
        "size_label": None,
    }


def _classify_exception(exc: Exception) -> tuple[str, str, str]:
    if isinstance(exc, FileNotFoundError):
        return ("storage_error", "file_not_found", "Uploaded file could not be found in storage")
    if isinstance(exc, PermissionError):
        return ("storage_error", "permission_denied", "Uploaded file could not be accessed in storage")
    if isinstance(exc, OSError):
        return ("storage_error", "storage_io_error", "Uploaded file could not be read from storage")
    if isinstance(exc, ValueError):
        return ("unreadable", "empty_or_invalid_pdf", "Uploaded PDF is empty or unreadable")

    message = str(exc).lower()
    class_name = exc.__class__.__name__.lower()

    if "password" in message or "encryption" in message or "unsupported" in message:
        return ("unsupported", "unsupported_pdf_format", "Unsupported PDF format")
    if (
        "filedataerror" in class_name
        or "not a pdf" in message
        or "cannot open broken document" in message
        or "malformed" in message
        or "format error" in message
        or "repair" in message
    ):
        return ("unreadable", "corrupt_or_unreadable_pdf", "PDF is unreadable or corrupt")

    return ("failed", "analysis_failed", "PDF analysis failed")


def _source_name(source: Any) -> str:
    if isinstance(source, (str, Path)):
        return str(source)
    name = getattr(source, "name", None)
    return str(name or source.__class__.__name__)


def _match_size(width_mm: float, height_mm: float) -> dict[str, Any] | None:
    best_match: dict[str, Any] | None = None
    best_diff = float("inf")
    for candidate in SIZE_CANDIDATES:
        diff = _size_diff(
            width_mm,
            height_mm,
            candidate["width_mm"],
            candidate["height_mm"],
        )
        if diff <= candidate["tolerance_mm"] and diff < best_diff:
            best_diff = diff
            best_match = candidate
    return best_match


def _suggest_product(
    *,
    pages: int,
    width_mm: float,
    height_mm: float,
    size_match: dict[str, Any] | None,
    warnings: list[str],
) -> dict[str, Any] | None:
    if _is_large_format(width_mm, height_mm):
        confidence = "high" if max(width_mm, height_mm) >= 500 else "medium"
        return {
            "type": "poster_large_format",
            "label": "Poster / Large Format",
            "confidence": confidence,
            "reason": f"PDF size is {width_mm} x {height_mm} mm, which looks larger than standard sheet sizes.",
        }

    if size_match and size_match["label"] in {"A4", "A5"} and pages > 4:
        confidence = "high" if pages % 4 == 0 else "medium"
        if pages % 4 != 0 and SADDLE_STITCH_WARNING not in warnings:
            warnings.append(SADDLE_STITCH_WARNING)
        return {
            "type": "booklet",
            "label": "Booklets",
            "confidence": confidence,
            "reason": f"PDF has {pages} pages and the size is close to {size_match['label']}, which looks like a booklet.",
        }

    if size_match and size_match["label"] == "Business Card":
        confidence = "high" if pages <= 2 else "medium"
        return {
            "type": "business_card",
            "label": "Business Cards",
            "confidence": confidence,
            "reason": f"PDF size is close to {size_match['width_mm']} x {size_match['height_mm']} mm and has {pages} page{'s' if pages != 1 else ''}.",
        }

    if size_match and size_match["label"] in {"A5", "A4", "DL"}:
        confidence = "high" if pages <= 2 else "medium"
        return {
            "type": "flyer",
            "label": "Flyers",
            "confidence": confidence,
            "reason": f"PDF size is close to {size_match['label']} and has {pages} page{'s' if pages != 1 else ''}.",
        }

    return None


def _build_suggestions(
    *,
    pages: int,
    suggested_product: dict[str, Any] | None,
    size_match: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not suggested_product:
        return []

    confidence = suggested_product["confidence"]
    suggestions: list[dict[str, Any]] = []
    product_type = suggested_product["type"]

    if product_type in {"business_card", "flyer", "booklet"}:
        suggestions.append(
            {
                "field": "product_type",
                "value": product_type,
                "label": suggested_product["label"],
                "confidence": confidence,
            }
        )

    if product_type == "booklet":
        suggestions.append(
            {
                "field": "total_pages",
                "value": pages,
                "label": f"{pages} pages",
                "confidence": confidence,
            }
        )

    if size_match and size_match.get("value") and product_type in {"business_card", "flyer", "booklet"}:
        suggestions.append(
            {
                "field": "finished_size",
                "value": size_match["value"],
                "label": size_match["display_label"],
                "confidence": confidence,
            }
        )

    return suggestions


def _is_large_format(width_mm: float, height_mm: float) -> bool:
    return width_mm > 297 or height_mm > 420 or (
        width_mm > STANDARD_SIZES["A3"][0] + LARGE_ITEM_TOLERANCE_MM
        and height_mm > STANDARD_SIZES["A3"][1] + LARGE_ITEM_TOLERANCE_MM
    )


def _size_diff(width_mm: float, height_mm: float, target_width_mm: float, target_height_mm: float) -> float:
    direct = max(abs(width_mm - target_width_mm), abs(height_mm - target_height_mm))
    rotated = max(abs(width_mm - target_height_mm), abs(height_mm - target_width_mm))
    return min(direct, rotated)
