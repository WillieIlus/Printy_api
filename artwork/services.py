from __future__ import annotations

from typing import Any


STANDARD_SIZES = {
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "A6": (105, 148),
    "DL": (99, 210),
    "Business Card": (85, 55),
}


def analyze_pdf(file_path: str) -> dict[str, Any]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {
            "analysis_status": "failed",
            "analysis_error": "PDF analysis unavailable",
            "detected": None,
            "suggestions": [],
            "confidence": "none",
            "warnings": ["We could not read PDF details automatically."],
        }

    try:
        doc = fitz.open(file_path)
    except Exception:
        return {
            "analysis_status": "failed",
            "analysis_error": "PDF could not be read",
            "detected": None,
            "suggestions": [],
            "confidence": "low",
            "warnings": ["We could not read PDF details automatically."],
        }

    if doc.page_count == 0:
        return {
            "analysis_status": "failed",
            "analysis_error": "PDF has no pages",
            "detected": None,
            "suggestions": [],
            "confidence": "low",
            "warnings": ["We could not read PDF details automatically."],
        }

    pages = doc.page_count
    widths: list[float] = []
    heights: list[float] = []
    warnings: list[str] = []

    for page in doc:
        rect = page.rect
        widths.append(rect.width)
        heights.append(rect.height)

    if len({round(w, 1) for w in widths}) > 1 or len({round(h, 1) for h in heights}) > 1:
        warnings.append("Mixed page sizes detected")

    width_pts = widths[0]
    height_pts = heights[0]
    width_mm = round(width_pts * 25.4 / 72)
    height_mm = round(height_pts * 25.4 / 72)

    # Rough bleed check: standard sizes usually land within ~3mm on each side
    size_label = _match_size(width_mm, height_mm)
    if size_label:
        std_w, std_h = STANDARD_SIZES[size_label]
        diff = abs(width_mm - std_w) + abs(height_mm - std_h)
        if diff < 2:
            warnings.append("No bleed detected")

    suggestions: list[dict[str, Any]] = [{"field": "pages", "value": pages}]
    if size_label:
        suggestions.append({"field": "size", "value": size_label})

    return {
        "analysis_status": "analysed",
        "analysis_error": None,
        "detected": {"pages": pages, "width_mm": width_mm, "height_mm": height_mm},
        "suggestions": suggestions,
        "confidence": "high" if not warnings else "medium",
        "warnings": warnings,
    }


def _match_size(width_mm: float, height_mm: float) -> str | None:
    best_key: str | None = None
    best_diff = float("inf")
    for label, (w, h) in STANDARD_SIZES.items():
        diff = min(
            abs(width_mm - w) + abs(height_mm - h),
            abs(width_mm - h) + abs(height_mm - w),
        )
        if diff < best_diff:
            best_diff = diff
            best_key = label
    return best_key if best_diff <= 12 else None
