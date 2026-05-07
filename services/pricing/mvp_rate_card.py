from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import OperationalError, ProgrammingError
from rest_framework.exceptions import ValidationError

from services.pricing.imposition import build_imposition_breakdown


DEFAULT_PAPER_DEFINITIONS: list[dict[str, Any]] = [
    {"key": "130gsm_matte_art", "id": "paper-130gsm-matte-art", "label": "130gsm Matte/Art", "paper_name": "130gsm", "gsm": 130, "paper_type": "Matte/Art", "category": "Matte/Art", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "34.00", "double_side_price": "58.00", "active": False},
    {"key": "150gsm_matte_art", "id": "paper-150gsm-matte-art", "label": "150gsm Matte/Art", "paper_name": "150gsm", "gsm": 150, "paper_type": "Matte/Art", "category": "Matte/Art", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "38.00", "double_side_price": "64.00", "active": False},
    {"key": "170gsm_matte_art", "id": "paper-170gsm-matte-art", "label": "170gsm Matte/Art", "paper_name": "170gsm", "gsm": 170, "paper_type": "Matte/Art", "category": "Matte/Art", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "42.00", "double_side_price": "70.00", "active": False},
    {"key": "200gsm_matte", "id": "paper-200gsm-matte", "label": "200gsm Matte", "paper_name": "200gsm", "gsm": 200, "paper_type": "Matte", "category": "Matte", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "46.00", "double_side_price": "76.00", "active": False},
    {"key": "220gsm_matte", "id": "paper-220gsm-matte", "label": "220gsm Matte", "paper_name": "220gsm", "gsm": 220, "paper_type": "Matte", "category": "Matte", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "49.00", "double_side_price": "81.00", "active": False},
    {"key": "250gsm_matte", "id": "paper-250gsm-matte", "label": "250gsm Matte", "paper_name": "250gsm", "gsm": 250, "paper_type": "Matte", "category": "Matte", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "54.00", "double_side_price": "88.00", "active": False},
    {"key": "300gsm_matte_art_card", "id": "paper-300gsm-matte-art-card", "label": "300gsm Matte/Art Card", "paper_name": "300gsm", "gsm": 300, "paper_type": "Matte/Art Card", "category": "Matte/Art Card", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "62.00", "double_side_price": "102.00", "active": False},
    {"key": "350gsm_matte_art_card", "id": "paper-350gsm-matte-art-card", "label": "350gsm Matte/Art Card", "paper_name": "350gsm", "gsm": 350, "paper_type": "Matte/Art Card", "category": "Matte/Art Card", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "82.00", "double_side_price": "132.00", "active": False},
    {"key": "250gsm_cover_board", "id": "paper-250gsm-cover-board", "label": "250gsm Cover Board", "paper_name": "250gsm", "gsm": 250, "paper_type": "Cover Board", "category": "Cover Board", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "68.00", "double_side_price": "108.00", "active": False},
    {"key": "300gsm_ivory", "id": "paper-300gsm-ivory", "label": "300gsm Ivory", "paper_name": "300gsm", "gsm": 300, "paper_type": "Ivory", "category": "Ivory", "size": "SRA3/A3", "supports_double_side": True, "single_side_price": "118.00", "double_side_price": "172.00", "active": False},
    {"key": "150gsm_tictac_sticker", "id": "paper-150gsm-tictac-sticker", "label": "150gsm Tictac/Sticker", "paper_name": "150gsm", "gsm": 150, "paper_type": "Tictac/Sticker", "category": "Tictac/Sticker", "size": "SRA3/A3", "supports_double_side": False, "single_side_price": "74.00", "double_side_price": None, "active": False},
]

DEFAULT_FINISHING_DEFINITIONS: list[dict[str, Any]] = [
    {"key": "matte_lamination", "id": "finishing-matte-lamination", "label": "Matte Lamination", "name": "Matte Lamination", "pricing_mode": "per_sheet", "unit": "sheet", "price": "38.00", "active": False},
    {"key": "gloss_lamination", "id": "finishing-gloss-lamination", "label": "Gloss Lamination", "name": "Gloss Lamination", "pricing_mode": "per_sheet", "unit": "sheet", "price": "36.00", "active": False},
    {"key": "cutting", "id": "finishing-cutting", "label": "Cutting", "name": "Cutting", "pricing_mode": "flat_per_job", "unit": "job", "price": "480.00", "active": False},
    {"key": "saddle_stitching", "id": "finishing-saddle-stitching", "label": "Saddle Stitching", "name": "Saddle Stitching", "pricing_mode": "flat_per_job", "unit": "job", "price": "460.00", "active": False},
    {"key": "perfect_binding", "id": "finishing-perfect-binding", "label": "Perfect Binding", "name": "Perfect Binding", "pricing_mode": "per_book", "unit": "book", "price": "240.00", "active": False},
    {"key": "spiral_binding", "id": "finishing-spiral-binding", "label": "Spiral Binding", "name": "Spiral Binding", "pricing_mode": "per_book", "unit": "book", "price": "230.00", "active": False},
    {"key": "round_cornering", "id": "finishing-round-cornering", "label": "Round Cornering", "name": "Round Cornering", "pricing_mode": "flat_per_job", "unit": "job", "price": "260.00", "active": False},
]

DEFAULT_SHOP_DETAILS = {
    "shop_name": "",
    "whatsapp_number": "",
    "location_area": "",
}


def _build_default_paper_rows() -> list[dict[str, Any]]:
    return deepcopy(DEFAULT_PAPER_DEFINITIONS)


def _build_default_finishing_rows() -> list[dict[str, Any]]:
    return deepcopy(DEFAULT_FINISHING_DEFINITIONS)


PAPER_DEFINITION_BY_KEY = {row["key"]: row for row in DEFAULT_PAPER_DEFINITIONS}
FINISHING_DEFINITION_BY_KEY = {row["key"]: row for row in DEFAULT_FINISHING_DEFINITIONS}

MARKET_GUIDE_MIN_SAMPLE_COUNT = 3
BUSINESS_CARD_WIDTH_MM = 90
BUSINESS_CARD_HEIGHT_MM = 55
SRA3_WIDTH_MM = 320
SRA3_HEIGHT_MM = 450


def _deepcopy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return deepcopy(rows)


def _to_decimal(value: Any, *, allow_null: bool = False) -> Decimal | None:
    if value in (None, ""):
        if allow_null:
            return None
        raise ValidationError("A numeric value is required.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("Enter a valid numeric amount.") from exc
    if amount < 0:
        raise ValidationError("Prices cannot be negative.")
    return amount.quantize(Decimal("0.01"))


def _decimal_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))


def _decimal_stats(values: list[Decimal]) -> dict[str, str | int | None]:
    if not values:
        return {"min": None, "max": None, "median": None, "mean": None, "sample_count": 0}
    ordered = sorted(values)
    count = len(ordered)
    midpoint = count // 2
    median = ordered[midpoint] if count % 2 == 1 else (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")
    mean = sum(ordered) / Decimal(count)
    return {
        "min": _decimal_string(ordered[0]),
        "max": _decimal_string(ordered[-1]),
        "median": _decimal_string(median),
        "mean": _decimal_string(mean),
        "sample_count": count,
    }


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _paper_row_from_definition(definition: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(definition)


def _finishing_row_from_definition(definition: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(definition)


def _resolve_paper_definition(row: dict[str, Any]) -> dict[str, Any] | None:
    key = _normalize_text(row.get("key"))
    if key and key in PAPER_DEFINITION_BY_KEY:
        return PAPER_DEFINITION_BY_KEY[key]

    row_id = _normalize_text(row.get("id"))
    if row_id:
        for definition in DEFAULT_PAPER_DEFINITIONS:
            if definition["id"] == row_id:
                return definition

    paper_name = _normalize_text(row.get("paper_name")).lower().replace(" ", "")
    paper_type = _normalize_text(row.get("paper_type")).lower()
    for definition in DEFAULT_PAPER_DEFINITIONS:
        definition_name = _normalize_text(definition.get("paper_name")).lower().replace(" ", "")
        definition_type = _normalize_text(definition.get("paper_type")).lower()
        if paper_name == definition_name and paper_type == definition_type:
            return definition
    return None


def _resolve_finishing_definition(row: dict[str, Any]) -> dict[str, Any] | None:
    key = _normalize_text(row.get("key"))
    if key and key in FINISHING_DEFINITION_BY_KEY:
        return FINISHING_DEFINITION_BY_KEY[key]

    row_id = _normalize_text(row.get("id"))
    if row_id:
        for definition in DEFAULT_FINISHING_DEFINITIONS:
            if definition["id"] == row_id:
                return definition

    name = _normalize_text(row.get("name")).lower()
    for definition in DEFAULT_FINISHING_DEFINITIONS:
        if _normalize_text(definition.get("name")).lower() == name:
            return definition
    return None


def _normalize_paper_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    input_rows = { _normalize_text(row.get("key")): row for row in (rows or []) if row.get("key") }
    normalized: list[dict[str, Any]] = []

    for definition in DEFAULT_PAPER_DEFINITIONS:
        row = input_rows.get(definition["key"])
        if row is None:
            # Re-add missing default as inactive
            normalized_row = _paper_row_from_definition(definition)
            normalized_row["active"] = False
            normalized.append(normalized_row)
            continue

        active = bool(row.get("active"))
        single = _to_decimal(row.get("single_side_price"), allow_null=not active)
        double = _to_decimal(row.get("double_side_price"), allow_null=True)
        if active and single is None:
            # Find index for error reporting
            idx = (rows or []).index(row)
            raise ValidationError({"paper_prices": {idx: {"single_side_price": ["Enter a valid non-negative single-side price."]}}})

        normalized_row = _paper_row_from_definition(definition)
        normalized_row.update(
            {
                "single_side_price": _decimal_string(single),
                "double_side_price": _decimal_string(double),
                "active": active,
            }
        )
        normalized.append(normalized_row)
    return normalized


def _normalize_finishing_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    input_rows = { _normalize_text(row.get("key")): row for row in (rows or []) if row.get("key") }
    normalized: list[dict[str, Any]] = []

    for definition in DEFAULT_FINISHING_DEFINITIONS:
        row = input_rows.get(definition["key"])
        if row is None:
            # Re-add missing default as inactive
            normalized_row = _finishing_row_from_definition(definition)
            normalized_row["active"] = False
            normalized.append(normalized_row)
            continue

        active = bool(row.get("active"))
        price = _to_decimal(row.get("price"), allow_null=not active)
        if active and price is None:
            idx = (rows or []).index(row)
            raise ValidationError({"finishings": {idx: {"price": ["Enter a valid non-negative finishing price."]}}})

        normalized_row = _finishing_row_from_definition(definition)
        normalized_row.update(
            {
                "price": _decimal_string(price),
                "active": active,
            }
        )
        normalized.append(normalized_row)
    return normalized


def _normalize_shop_details(details: dict[str, Any] | None) -> dict[str, str]:
    payload = deepcopy(DEFAULT_SHOP_DETAILS)
    for key in payload:
        payload[key] = _normalize_text((details or {}).get(key))
    return payload


def _is_active_paper(row: dict[str, Any]) -> bool:
    return bool(row.get("active")) and row.get("single_side_price") not in (None, "")


def _is_active_finishing(row: dict[str, Any]) -> bool:
    return bool(row.get("active")) and row.get("price") not in (None, "")


def _paper_matches(row: dict[str, Any], *, names: tuple[str, ...] = (), gsms: tuple[int, ...] = (), paper_types: tuple[str, ...] = ()) -> bool:
    if not _is_active_paper(row):
        return False
    paper_name = _normalize_text(row.get("paper_name")).lower()
    paper_type = _normalize_text(row.get("paper_type")).lower()
    label = _normalize_text(row.get("label")).lower()
    category = _normalize_text(row.get("category")).lower()
    gsm = row.get("gsm")
    return (
        (bool(names) and any(name in paper_name or name in label for name in names))
        or (bool(gsms) and gsm in gsms)
        or (bool(paper_types) and any(item in paper_type or item in category for item in paper_types))
    )


def _has_finishing(rows: list[dict[str, Any]], names: tuple[str, ...]) -> bool:
    for row in rows:
        if _is_active_finishing(row) and any(name in _normalize_text(row.get("name")).lower() for name in names):
            return True
    return False


def _build_unlocked_products(paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    unlocked: list[dict[str, str]] = []

    heavy_stock = any(_paper_matches(row, gsms=(300, 350), names=("300", "350")) for row in paper_rows)
    light_stock = any(_paper_matches(row, gsms=(130, 150, 170), names=("130", "150", "170")) for row in paper_rows)
    any_paper = any(_is_active_paper(row) for row in paper_rows)
    sticker_stock = any(_paper_matches(row, names=("tic tac", "sticker", "tictac"), paper_types=("sticker",)) for row in paper_rows)

    has_cutting = _has_finishing(finishing_rows, ("cutting",))
    has_lamination = _has_finishing(finishing_rows, ("lamination",))
    has_saddle = _has_finishing(finishing_rows, ("saddle",))
    has_perfect = _has_finishing(finishing_rows, ("perfect",))
    has_spiral = _has_finishing(finishing_rows, ("spiral",))

    if heavy_stock and has_cutting:
        unlocked.append({"key": "business-cards", "label": "Business Cards", "reason": "Heavy card stock plus cutting is ready."})
    if heavy_stock and has_lamination and has_cutting:
        unlocked.append({"key": "laminated-business-cards", "label": "Laminated Business Cards", "reason": "Card stock, lamination, and cutting are ready."})
    if light_stock:
        unlocked.append({"key": "flyers", "label": "Flyers", "reason": "Light digital paper is ready."})
        unlocked.append({"key": "posters", "label": "Posters", "reason": "Light digital paper is ready."})
    if light_stock and has_cutting:
        unlocked.append({"key": "brochures", "label": "Brochures", "reason": "Light paper plus cutting is ready."})
    if any_paper and has_saddle:
        unlocked.append({"key": "booklets", "label": "Booklets", "reason": "Paper plus saddle stitching is ready."})
    if any_paper and has_perfect:
        unlocked.append({"key": "perfect-bound-books", "label": "Perfect Bound Books", "reason": "Paper plus perfect binding is ready."})
    if any_paper and has_spiral:
        unlocked.append({"key": "spiral-bound-reports", "label": "Spiral Bound Reports", "reason": "Paper plus spiral binding is ready."})
    if sticker_stock and has_cutting:
        unlocked.append({"key": "stickers", "label": "Stickers", "reason": "Sticker stock plus cutting is ready."})

    return unlocked


def _market_guide_or_placeholder(values: list[Decimal]) -> dict[str, Any]:
    stats = _decimal_stats(values)
    enough = len(values) >= MARKET_GUIDE_MIN_SAMPLE_COUNT
    return {
        "min": stats["min"] if enough else None,
        "max": stats["max"] if enough else None,
        "median": stats["median"] if enough else None,
        "mean": stats["mean"] if enough else None,
        "sample_count": stats["sample_count"],
        "has_enough_data": enough,
        "message": None if enough else "Market guide appears after enough anonymous shop samples.",
    }


def _iter_saved_rate_cards():
    from shops.models import Shop
    return Shop.objects.exclude(mvp_rate_card__isnull=True).exclude(mvp_rate_card={}).only("id", "mvp_rate_card")


def _safe_saved_rate_cards() -> list[Any]:
    try:
        return list(_iter_saved_rate_cards())
    except (ProgrammingError, OperationalError):
        return []


def build_market_guides(paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    guides: dict[str, dict[str, Any]] = {}
    saved_shops = _safe_saved_rate_cards()

    for row in paper_rows:
        single_values: list[Decimal] = []
        double_values: list[Decimal] = []
        for shop in saved_shops:
            rate_card = getattr(shop, "mvp_rate_card", None) or {}
            for saved_row in rate_card.get("paper_rows") or []:
                if not saved_row.get("active"):
                    continue
                if _normalize_text(saved_row.get("key")) != _normalize_text(row.get("key")):
                    continue
                try:
                    single_values.append(_to_decimal(saved_row.get("single_side_price")))
                    if saved_row.get("double_side_price") not in (None, ""):
                        parsed_double = _to_decimal(saved_row.get("double_side_price"), allow_null=True)
                        if parsed_double is not None:
                            double_values.append(parsed_double)
                except ValidationError:
                    continue
        guide = {
            "single_side_price": _market_guide_or_placeholder(single_values),
            "double_side_price": _market_guide_or_placeholder(double_values),
        }
        guides[row["key"]] = guide
        guides[row["id"]] = guide

    for row in finishing_rows:
        values: list[Decimal] = []
        for shop in saved_shops:
            rate_card = getattr(shop, "mvp_rate_card", None) or {}
            for saved_row in rate_card.get("finishing_rows") or []:
                if not saved_row.get("active"):
                    continue
                if _normalize_text(saved_row.get("key")) != _normalize_text(row.get("key")):
                    continue
                try:
                    values.append(_to_decimal(saved_row.get("price")))
                except ValidationError:
                    continue
        guide = {"price": _market_guide_or_placeholder(values)}
        guides[row["key"]] = guide
        guides[row["id"]] = guide

    return guides


def build_business_card_example(paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_paper = next(
        (
            row for row in paper_rows
            if _is_active_paper(row) and (row.get("gsm") in (300, 350) or any(token in _normalize_text(row.get("paper_name")).lower() for token in ("300", "350")))
        ),
        None,
    )
    lamination = next((row for row in finishing_rows if _is_active_finishing(row) and "lamination" in _normalize_text(row.get("name")).lower()), None)
    cutting = next((row for row in finishing_rows if _is_active_finishing(row) and "cutting" in _normalize_text(row.get("name")).lower()), None)

    imposition = build_imposition_breakdown(
        quantity=100,
        finished_width_mm=BUSINESS_CARD_WIDTH_MM,
        finished_height_mm=BUSINESS_CARD_HEIGHT_MM,
        sheet_width_mm=SRA3_WIDTH_MM,
        sheet_height_mm=SRA3_HEIGHT_MM,
    )
    sheets_needed = imposition.good_sheets or 5

    missing_fields: list[str] = []
    if not candidate_paper or candidate_paper.get("double_side_price") in (None, ""):
        missing_fields.append("300gsm double price")
    if lamination is None:
        missing_fields.append("lamination")
    if cutting is None:
        missing_fields.append("cutting")

    print_total = Decimal("0.00")
    lamination_total = Decimal("0.00")
    cutting_total = Decimal("0.00")

    if candidate_paper and candidate_paper.get("double_side_price") not in (None, ""):
        print_total = (_to_decimal(candidate_paper.get("double_side_price"), allow_null=True) or Decimal("0")) * Decimal(sheets_needed)
    if lamination is not None:
        lamination_total = _to_decimal(lamination.get("price")) * Decimal(sheets_needed)
    if cutting is not None:
        cutting_total = _to_decimal(cutting.get("price"))

    estimated_total = print_total + lamination_total + cutting_total
    is_complete = not missing_fields
    return {
        "title": "Example: 100 business cards",
        "paper_label": candidate_paper.get("label") if candidate_paper else "300gsm or 350gsm double-sided",
        "sheets_needed": sheets_needed,
        "missing_fields": missing_fields,
        "is_complete": is_complete,
        "is_active": bool(candidate_paper or lamination or cutting),
        "status_text": "Quote proof is ready." if is_complete else f"Waiting for {', '.join(missing_fields)}...",
        "line_items": [
            {
                "key": "print",
                "label": "Print",
                "active": print_total > 0,
                "detail": (
                    f"{sheets_needed} SRA3 sheets x KES {candidate_paper.get('double_side_price')} double-sided"
                    if candidate_paper and candidate_paper.get("double_side_price") not in (None, "") else
                    "Waiting for 300gsm or 350gsm double-sided price"
                ),
                "total": _decimal_string(print_total) if print_total > 0 else None,
            },
            {
                "key": "lamination",
                "label": "Matte lamination",
                "active": lamination is not None,
                "detail": (
                    f"{sheets_needed} sheets x KES {lamination.get('price')}"
                    if lamination is not None else
                    "Waiting for lamination price"
                ),
                "total": _decimal_string(lamination_total) if lamination is not None else None,
            },
            {
                "key": "cutting",
                "label": "Cutting",
                "active": cutting is not None,
                "detail": (
                    f"Flat rate x KES {cutting.get('price')}"
                    if cutting is not None else
                    "Waiting for cutting price"
                ),
                "total": _decimal_string(cutting_total) if cutting is not None else None,
            },
        ],
        "estimated_total": _decimal_string(estimated_total) if estimated_total > 0 else None,
    }


def _build_completion_feed(paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]]) -> list[str]:
    feed: list[str] = []
    if any(_paper_matches(row, gsms=(300,), names=("300",)) for row in paper_rows):
        feed.append("Add 300gsm Matte pricing -> Now you can price business cards, cards, covers")
    if _has_finishing(finishing_rows, ("matt lamination", "matte lamination")):
        feed.append("Add Matte Lamination -> Now you can price laminated business cards, menus, covers")
    if _has_finishing(finishing_rows, ("cutting",)):
        feed.append("Add Cutting -> Now you can price finished business cards and flyers")
    if any(_paper_matches(row, gsms=(150, 170), names=("150", "170")) for row in paper_rows):
        feed.append("Add 150gsm / 170gsm -> Now you can price flyers, posters, brochures")
    if _has_finishing(finishing_rows, ("saddle",)):
        feed.append("Add Saddle Stitching -> Now you can price booklets")
    if _has_finishing(finishing_rows, ("perfect", "spiral")):
        feed.append("Add Perfect/Spiral Binding -> Now you can price books, reports, proposals")
    return feed


def _build_next_suggestions(paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]]) -> list[str]:
    suggestions: list[str] = []
    if not any(_paper_matches(row, gsms=(300, 350), names=("300", "350")) for row in paper_rows):
        suggestions.append("Start with 300gsm or 350gsm card stock so business cards unlock first.")
    if not _has_finishing(finishing_rows, ("cutting",)):
        suggestions.append("Add Cutting next so finished cards and flyers become quote-ready.")
    if not any(_paper_matches(row, gsms=(130, 150, 170), names=("130", "150", "170")) for row in paper_rows):
        suggestions.append("Add 130gsm, 150gsm, or 170gsm next for flyers and brochures.")
    if not _has_finishing(finishing_rows, ("matt lamination", "matte lamination", "gloss lamination")):
        suggestions.append("Add lamination to unlock premium card work.")
    if not _has_finishing(finishing_rows, ("saddle", "perfect", "spiral")):
        suggestions.append("Add at least one binding rule for booklets and reports.")
    return suggestions[:3]


def summarize_rate_card(paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]]) -> dict[str, Any]:
    active_paper_rows = [row for row in paper_rows if _is_active_paper(row)]
    active_finishing_rows = [row for row in finishing_rows if _is_active_finishing(row)]
    unlocked = _build_unlocked_products(paper_rows, finishing_rows)
    return {
        "pricing_items_added": len(active_paper_rows) + len(active_finishing_rows),
        "paper_rows_added": len(active_paper_rows),
        "finishing_rows_added": len(active_finishing_rows),
        "products_unlocked": len(unlocked),
        "unlocked_products": unlocked,
        "completion_feed": _build_completion_feed(paper_rows, finishing_rows),
        "next_suggestions": _build_next_suggestions(paper_rows, finishing_rows),
    }


def build_public_rate_card_builder_config() -> dict[str, Any]:
    paper_rows = _build_default_paper_rows()
    finishing_rows = _build_default_finishing_rows()
    summary = summarize_rate_card(paper_rows, finishing_rows)
    return {
        "paper_rows": paper_rows,
        "finishing_rows": finishing_rows,
        "shop_details": deepcopy(DEFAULT_SHOP_DETAILS),
        "summary": summary,
        "market_guides": build_market_guides(paper_rows, finishing_rows),
        "example_quote": build_business_card_example(paper_rows, finishing_rows),
        "market_label": "Nairobi Market Guide",
    }


def preview_public_rate_card_builder(*, paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_papers = _normalize_paper_rows(paper_rows)
    normalized_finishings = _normalize_finishing_rows(finishing_rows)
    return {
        "paper_rows": normalized_papers,
        "finishing_rows": normalized_finishings,
        "summary": summarize_rate_card(normalized_papers, normalized_finishings),
        "market_guides": build_market_guides(normalized_papers, normalized_finishings),
        "example_quote": build_business_card_example(normalized_papers, normalized_finishings),
        "market_label": "Nairobi Market Guide",
    }


def build_shop_rate_card_setup(shop) -> dict[str, Any]:
    saved = shop.mvp_rate_card or {}
    paper_rows = _normalize_paper_rows(saved.get("paper_rows") or _build_default_paper_rows())
    finishing_rows = _normalize_finishing_rows(saved.get("finishing_rows") or _build_default_finishing_rows())
    shop_details = _normalize_shop_details(
        saved.get("shop_details")
        or {
            "shop_name": getattr(shop, "name", ""),
            "whatsapp_number": getattr(shop, "public_whatsapp_number", "") or getattr(shop, "phone_number", ""),
            "location_area": getattr(shop, "service_area", "") or getattr(shop, "city", ""),
        }
    )
    return {
        "paper_rows": paper_rows,
        "finishing_rows": finishing_rows,
        "shop_details": shop_details,
        "summary": summarize_rate_card(paper_rows, finishing_rows),
        "market_guides": build_market_guides(paper_rows, finishing_rows),
        "example_quote": build_business_card_example(paper_rows, finishing_rows),
        "market_label": "Nairobi Market Guide",
        "completed": bool(saved.get("completed")),
    }


def save_shop_rate_card_setup(shop, *, paper_rows: list[dict[str, Any]], finishing_rows: list[dict[str, Any]], shop_details: dict[str, Any] | None = None, completed: bool | None = None) -> dict[str, Any]:
    normalized_papers = _normalize_paper_rows(paper_rows)
    normalized_finishings = _normalize_finishing_rows(finishing_rows)
    normalized_details = _normalize_shop_details(shop_details)
    summary = summarize_rate_card(normalized_papers, normalized_finishings)

    payload = {
        "paper_rows": normalized_papers,
        "finishing_rows": normalized_finishings,
        "shop_details": normalized_details,
        "summary": summary,
        "market_guides": build_market_guides(normalized_papers, normalized_finishings),
        "example_quote": build_business_card_example(normalized_papers, normalized_finishings),
        "market_label": "Nairobi Market Guide",
        "completed": bool(completed),
    }
    shop.mvp_rate_card = payload

    if normalized_details["shop_name"]:
        shop.name = normalized_details["shop_name"]
    if normalized_details["whatsapp_number"]:
        shop.public_whatsapp_number = normalized_details["whatsapp_number"]
        if not _normalize_text(getattr(shop, "phone_number", "")):
            shop.phone_number = normalized_details["whatsapp_number"]
    if normalized_details["location_area"]:
        shop.service_area = normalized_details["location_area"]
        if not _normalize_text(getattr(shop, "city", "")):
            shop.city = normalized_details["location_area"]

    shop.save(update_fields=["mvp_rate_card", "name", "public_whatsapp_number", "phone_number", "service_area", "city", "updated_at"])
    return payload


def complete_shop_rate_card_setup(shop) -> dict[str, Any]:
    current = build_shop_rate_card_setup(shop)
    payload = save_shop_rate_card_setup(
        shop,
        paper_rows=current["paper_rows"],
        finishing_rows=current["finishing_rows"],
        shop_details=current["shop_details"],
        completed=True,
    )
    return {
        "completed": True,
        "summary": payload["summary"],
        "shop_details": payload["shop_details"],
    }
