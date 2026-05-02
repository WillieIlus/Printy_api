from __future__ import annotations

from pathlib import Path
from typing import Any


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


def analyze_pdf(file_path: str) -> dict[str, Any]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return _failed_result(
            error="PDF analysis unavailable",
            warnings=["We could not read PDF details automatically."],
            confidence="none",
        )

    try:
        with fitz.open(file_path) as doc:
            if doc.page_count == 0:
                return _failed_result(
                    error="PDF has no pages",
                    warnings=["We could not read PDF details automatically."],
                )

            first_page = doc[0]
            width_mm = round(first_page.rect.width * 25.4 / 72, 1)
            height_mm = round(first_page.rect.height * 25.4 / 72, 1)

            sizes = {
                (round(page.rect.width, 1), round(page.rect.height, 1))
                for page in doc
            }
            warnings: list[str] = []
            if len(sizes) > 1:
                warnings.append("Mixed page sizes detected")

            preview_path = _render_preview(first_page, Path(file_path))
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
                "detected": {
                    "pages": doc.page_count,
                    "width_mm": width_mm,
                    "height_mm": height_mm,
                    "size_label": size_label,
                },
                "pages": doc.page_count,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "preview_path": str(preview_path) if preview_path else None,
                "preview_format": "jpeg" if preview_path else None,
                "suggested_product": suggested_product,
                "suggestions": suggestions,
                "confidence": suggested_product["confidence"] if suggested_product else ("high" if not warnings else "medium"),
                "warnings": warnings,
                "analysis_warnings": warnings,
                "size_label": size_label,
            }
    except Exception as exc:
        return _failed_result(
            error=str(exc) or "PDF could not be read",
            warnings=["We could not read PDF details automatically."],
        )


def _render_preview(page: Any, file_path: Path) -> Path | None:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None

    preview_path = file_path.with_suffix(".preview.jpg")
    pix = page.get_pixmap(matrix=fitz.Matrix(0.35, 0.35), alpha=False)
    pix.save(preview_path)
    return preview_path


def _failed_result(
    *,
    error: str,
    warnings: list[str],
    confidence: str = "low",
) -> dict[str, Any]:
    return {
        "analysis_status": "failed",
        "analysis_error": error,
        "detected": None,
        "pages": None,
        "width_mm": None,
        "height_mm": None,
        "preview_path": None,
        "preview_format": None,
        "suggested_product": None,
        "suggestions": [],
        "confidence": confidence,
        "warnings": warnings,
        "analysis_warnings": warnings,
        "size_label": None,
    }


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
