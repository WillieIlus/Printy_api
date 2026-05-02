from __future__ import annotations

from copy import deepcopy
from typing import Any

from services.public_matching import get_booklet_marketplace_matches, get_marketplace_matches

from .calculator_config import get_product_definition, resolve_finished_size, resolve_stock_option


def _parse_tier_gsm(raw: str | None) -> int | None:
    if not raw or not raw.endswith("gsm"):
        return None
    try:
        return int(raw[:-3])
    except ValueError:
        return None


PRODUCT_FAMILY_BY_TYPE = {
    "business_card": "flat",
    "flyer": "flat",
    "label_sticker": "flat",
    "letterhead": "flat",
    "booklet": "booklet",
    "large_format": "large_format",
}


def _has_requested_paper(payload: dict[str, Any], *, booklet: bool = False, prefix: str = "") -> bool:
    if booklet:
        return bool(payload.get(f"{prefix}_stock")) or bool(payload.get(f"requested_{prefix}_paper_category")) or bool(payload.get(f"requested_{prefix}_gsm"))
    return bool(payload.get("paper_stock")) or bool(payload.get("requested_paper_category")) or bool(payload.get("requested_gsm"))


def _required_missing(payload: dict[str, Any], definition: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in definition["required_fields"]:
        if field == "paper_stock":
            if not _has_requested_paper(payload):
                missing.append(field)
            continue
        if field == "cover_stock":
            if not _has_requested_paper(payload, booklet=True, prefix="cover"):
                missing.append(field)
            continue
        if field == "insert_stock":
            if not _has_requested_paper(payload, booklet=True, prefix="insert"):
                missing.append(field)
            continue
        value = payload.get(field)
        if value in (None, "", []):
            missing.append(field)
    return missing


def _build_missing_response(product_type: str, missing_fields: list[str]) -> dict[str, Any]:
    field_labels = {
        "paper_stock": "paper stock or requested paper",
        "cover_stock": "cover stock or requested cover paper",
        "insert_stock": "insert stock or requested insert paper",
        "finished_size": "finished size",
        "print_sides": "print sides",
        "color_mode": "color mode",
        "total_pages": "total pages",
        "material_type": "material",
        "width_mm": "width",
        "height_mm": "height",
    }
    readable = [field_labels.get(field, field.replace("_", " ")) for field in missing_fields]
    message = f"Choose {', '.join(readable)} to price this {product_type.replace('_', ' ')}."
    return {
        "mode": "calculator_public_preview",
        "can_calculate": False,
        "product_type": product_type,
        "price_mode": None,
        "total": None,
        "breakdown": None,
        "currency": "KES",
        "missing_fields": missing_fields,
        "missing_requirements": missing_fields,
        "warnings": [],
        "assumptions": [],
        "message": message,
        "summary": message,
        "matches": [],
        "shops": [],
        "selected_shops": [],
        "matches_count": 0,
        "min_price": None,
        "max_price": None,
        "production_preview": None,
        "pricing_breakdown": None,
        "unsupported_reasons": [],
        "suggestions": [],
        "exact_or_estimated": False,
    }


def _build_match_note(*, requested_category: str | None, requested_gsm: int | None, matched_label: str | None) -> dict[str, Any] | None:
    if not matched_label:
        return None
    requested_bits = []
    if requested_category:
        requested_bits.append(requested_category.replace("_", " ").title())
    if requested_gsm:
        requested_bits.append(f"{requested_gsm}gsm")
    requested_paper = " ".join(requested_bits).strip() or None
    if not requested_paper:
        return None
    if requested_paper.lower() in matched_label.lower():
        return {
            "requested_paper": requested_paper,
            "matched_paper": matched_label,
            "match_note": "Exact available stock",
            "fit_indicator": "exact",
        }
    return {
        "requested_paper": requested_paper,
        "matched_paper": matched_label,
        "match_note": "Closest available stock",
        "fit_indicator": "closest",
    }


def _attach_flat_match_metadata(response: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_category = payload.get("paper_type")
    requested_gsm = payload.get("paper_gsm")
    updated = deepcopy(response)
    for row in updated.get("matches", []):
        selection = row.get("selection") or {}
        preview = row.get("preview") or {}
        match = _build_match_note(
            requested_category=requested_category,
            requested_gsm=requested_gsm,
            matched_label=selection.get("paper_label"),
        )
        if match:
            preview["matched_stock"] = match
            row["preview"] = preview
    return updated


def _attach_booklet_match_metadata(response: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(response)
    for row in updated.get("matches", []):
        selection = row.get("selection") or {}
        preview = row.get("preview") or {}
        matches = []
        cover_match = _build_match_note(
            requested_category=payload.get("cover_paper_type"),
            requested_gsm=payload.get("cover_paper_gsm"),
            matched_label=selection.get("cover_paper_label"),
        )
        insert_match = _build_match_note(
            requested_category=payload.get("insert_paper_type"),
            requested_gsm=payload.get("insert_paper_gsm"),
            matched_label=selection.get("insert_paper_label"),
        )
        if cover_match:
            matches.append({"slot": "cover", **cover_match})
        if insert_match:
            matches.append({"slot": "insert", **insert_match})
        if matches:
            preview["matched_stock"] = matches
            row["preview"] = preview
    return updated


def _extract_production_preview(matches: list[dict[str, Any]], product_type: str) -> dict[str, Any] | None:
    if not matches:
        return None

    top_match = matches[0]
    preview_data = top_match.get("preview") or {}
    breakdown = preview_data.get("breakdown") or {}
    imposition = preview_data.get("imposition") or breakdown.get("imposition") or {}
    paper = breakdown.get("paper") or {}
    finishing_rows = breakdown.get("finishings") or preview_data.get("finishings") or []
    warnings = preview_data.get("explanations") or preview_data.get("warnings") or []

    result: dict[str, Any] = {
        "pieces_per_sheet": imposition.get("copies_per_sheet") or preview_data.get("copies_per_sheet"),
        "sheets_required": imposition.get("good_sheets") or preview_data.get("good_sheets"),
        "parent_sheet": imposition.get("sheet_size") or imposition.get("sheet_name") or paper.get("sheet_size") or preview_data.get("parent_sheet_name"),
        "imposition_label": imposition.get("explanation") or preview_data.get("reason"),
        "size_label": paper.get("label") or paper.get("sheet_size"),
        "quantity": preview_data.get("quantity"),
        "cutting_required": True if product_type in ["business_card", "flyer", "label_sticker"] else None,
        "selected_finishings": [f.get("name") for f in finishing_rows if f.get("name")],
        "suggested_finishings": [],
        "warnings": warnings,
    }

    if product_type == "large_format":
        roll_usage = breakdown.get("roll_usage") or {}
        dimensions = breakdown.get("dimensions") or {}
        pricing = breakdown.get("pricing") or {}
        result.update({
            "size_label": preview_data.get("size_label") or result.get("size_label"),
            "roll_width_m": (
                round(float(roll_usage.get("roll_width_mm")) / 1000, 3)
                if roll_usage.get("roll_width_mm") not in (None, "")
                else None
            ),
            "roll_width_mm": roll_usage.get("roll_width_mm"),
            "items_per_row": roll_usage.get("items_per_row") or preview_data.get("items_per_row"),
            "rows": roll_usage.get("rows") or preview_data.get("rows"),
            "used_length_m": preview_data.get("used_length_m"),
            "orientation": roll_usage.get("orientation") or preview_data.get("orientation"),
            "input_size_m": {
                "width": round(float(dimensions.get("width_mm")) / 1000, 3),
                "height": round(float(dimensions.get("height_mm")) / 1000, 3),
            } if dimensions.get("width_mm") and dimensions.get("height_mm") else None,
            "charged_area_m2": preview_data.get("charged_area_m2") or pricing.get("charged_area_m2"),
            "printed_area_m2": preview_data.get("printed_area_m2"),
            "waste_area_m2": preview_data.get("waste_area_m2"),
            "overlap_area_m2": preview_data.get("overlap_area_m2"),
            "tiling": preview_data.get("tiling") or breakdown.get("tiling"),
        })
        return result

    if product_type == "booklet":
        booklet_bd = breakdown.get("booklet") or {}
        cover_bd = breakdown.get("cover") or {}
        insert_bd = breakdown.get("inserts") or {}
        result.update({
            "booklet_input_pages": preview_data.get("input_pages") or booklet_bd.get("requested_pages"),
            "booklet_normalized_pages": preview_data.get("normalized_pages") or booklet_bd.get("normalized_pages"),
            "booklet_blank_pages_added": preview_data.get("blank_pages_added") or booklet_bd.get("blank_pages_added"),
            "booklet_cover_pages": preview_data.get("cover_pages") or booklet_bd.get("cover_pages"),
            "booklet_insert_pages": preview_data.get("insert_pages") or booklet_bd.get("insert_pages"),
            "booklet_cover_sheets": preview_data.get("cover_sheets") or booklet_bd.get("cover_sheets"),
            "booklet_insert_sheets": preview_data.get("insert_sheets") or booklet_bd.get("insert_sheets"),
            "booklet_binding_label": booklet_bd.get("binding_label"),
            "booklet_cover_paper_label": (cover_bd.get("paper") or {}).get("label"),
            "booklet_insert_paper_label": (insert_bd.get("paper") or {}).get("label"),
        })

    return result


def _extract_pricing_breakdown(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not matches:
        return None

    top_match = matches[0]
    preview_data = top_match.get("preview") or {}
    breakdown = preview_data.get("breakdown") or {}
    per_sheet = preview_data.get("per_sheet_pricing") or breakdown.get("per_sheet_pricing") or {}
    totals = preview_data.get("totals") or {}
    line_items = (preview_data.get("calculation_result") or {}).get("line_items") or []
    pricing = breakdown.get("pricing") or preview_data.get("pricing") or {}
    material = breakdown.get("material") or {}
    material_rate = pricing.get("rate") if pricing.get("rate") is not None else material.get("rate_per_unit")

    return {
        "currency": top_match.get("currency", "KES"),
        "paper_price": per_sheet.get("paper_price"),
        "print_price_front": per_sheet.get("print_price_front"),
        "print_price_back": per_sheet.get("print_price_back"),
        "total_per_sheet": per_sheet.get("total_per_sheet"),
        "estimated_total": totals.get("grand_total"),
        "price_range": None,
        "formula": per_sheet.get("formula"),
        "method": pricing.get("method"),
        "rate": material_rate,
        "charged_area_m2": pricing.get("charged_area_m2"),
        "charged_length_m": pricing.get("charged_length_m"),
        "minimum_charge": pricing.get("minimum_charge"),
        "minimum_charge_applied": pricing.get("minimum_charge_applied"),
        "lines": [
            {
                "label": item.get("label") or item.get("code") or "Line item",
                "amount": item.get("amount"),
                "formula": item.get("formula"),
            }
            for item in line_items
        ],
    }


def build_public_calculator_preview(payload: dict[str, Any]) -> dict[str, Any]:
    product_type = (payload.get("product_type") or "").strip()
    definition = get_product_definition(product_type)
    if not definition:
        msg = "Select a supported product type."
        return {
            "mode": "calculator_public_preview",
            "can_calculate": False,
            "product_type": product_type or None,
            "price_mode": None,
            "missing_fields": ["product_type"],
            "missing_requirements": ["product_type"],
            "warnings": [],
            "assumptions": [],
            "message": msg,
            "summary": msg,
            "matches": [],
            "shops": [],
            "selected_shops": [],
            "matches_count": 0,
            "min_price": None,
            "max_price": None,
            "production_preview": None,
            "pricing_breakdown": None,
            "unsupported_reasons": [],
            "suggestions": [],
            "exact_or_estimated": False,
        }

    missing_fields = _required_missing(payload, definition)
    if missing_fields:
        return _build_missing_response(product_type, missing_fields)

    if product_type == "large_format":
        width_mm = payload.get("width_mm")
        height_mm = payload.get("height_mm")
        extra_missing: list[str] = []
        if not width_mm:
            extra_missing.append("width_mm")
        if not height_mm:
            extra_missing.append("height_mm")
        if extra_missing:
            return _build_missing_response(product_type, extra_missing)

        request_payload = {
            "calculator_mode": "marketplace",
            "product_family": "large_format",
            "pricing_mode": "custom",
            "product_pricing_mode": "LARGE_FORMAT",
            "product_subtype": payload.get("product_subtype") or definition["defaults"].get("product_subtype") or "banner",
            "quantity": payload.get("quantity"),
            "width_mm": width_mm,
            "height_mm": height_mm,
            "material_type": payload.get("material_type"),
            "finishing_slugs": [
                value
                for value in [
                    payload.get("lamination") if payload.get("lamination") not in {None, "", "none"} else None,
                    payload.get("cut_type"),
                ]
                if value
            ],
            "turnaround_hours": payload.get("turnaround_hours"),
        }
        response = get_marketplace_matches(request_payload)
        response["product_type"] = product_type
        response["can_calculate"] = bool(response.get("matches_count"))
        response["price_mode"] = "exact" if response.get("exact_or_estimated") else "estimate"
        response["missing_fields"] = response.get("missing_requirements", [])
        response["production_preview"] = response.get("production_preview")
        response["pricing_breakdown"] = response.get("pricing_breakdown")
        return response

    finished_size_raw = payload.get("finished_size") or ""
    custom_warnings: list[str] = []

    paper_selection_mode = payload.get("paper_selection_mode", "configured")
    if paper_selection_mode == "custom_request":
        custom_warnings.append("Requested paper needs shop confirmation.")

    if finished_size_raw == "custom":
        custom_width = payload.get("custom_width_mm") or payload.get("width_mm")
        custom_height = payload.get("custom_height_mm") or payload.get("height_mm")
        if not custom_width or not custom_height:
            return _build_missing_response(product_type, ["custom_width_mm", "custom_height_mm"])
        size = {"width_mm": float(custom_width), "height_mm": float(custom_height)}
        custom_warnings.append("Custom size will be priced from actual dimensions.")
    else:
        size = resolve_finished_size(product_type, finished_size_raw)
        if not size:
            return _build_missing_response(product_type, ["finished_size"])

    if product_type == "booklet":
        cover_stock_raw = payload.get("cover_stock") or ""
        insert_stock_raw = payload.get("insert_stock") or ""
        cover_stock = resolve_stock_option(cover_stock_raw, usage="cover")
        insert_stock = resolve_stock_option(insert_stock_raw, usage="insert")
        cover_tier_gsm = _parse_tier_gsm(cover_stock_raw) if not cover_stock else None
        insert_tier_gsm = _parse_tier_gsm(insert_stock_raw) if not insert_stock else None
        color_mode = payload.get("color_mode") or "COLOR"
        request_payload = {
            "product_family": "booklet",
            "quantity": payload.get("quantity"),
            "total_pages": payload.get("total_pages"),
            "binding_type": payload.get("binding_type") or definition["defaults"].get("binding_type"),
            "cover_paper_type": payload.get("requested_cover_paper_category") or (cover_stock or {}).get("category"),
            "cover_paper_gsm": payload.get("requested_cover_gsm") or (cover_stock or {}).get("gsm") or cover_tier_gsm,
            "insert_paper_type": payload.get("requested_insert_paper_category") or (insert_stock or {}).get("category"),
            "insert_paper_gsm": payload.get("requested_insert_gsm") or (insert_stock or {}).get("gsm") or insert_tier_gsm,
            "cover_lamination_mode": payload.get("cover_lamination") or definition["defaults"].get("cover_lamination"),
            "color_mode": color_mode,
            "width_mm": size["width_mm"],
            "height_mm": size["height_mm"],
            "turnaround_hours": payload.get("turnaround_hours"),
        }
        response = get_booklet_marketplace_matches(request_payload)
        response["product_type"] = product_type
        response["can_calculate"] = bool(response.get("matches_count"))
        response["price_mode"] = "exact" if response.get("exact_or_estimated") else "estimate"
        response["missing_fields"] = response.get("missing_requirements", [])
        if custom_warnings:
            response["warnings"] = list(response.get("warnings") or []) + custom_warnings

        # Attach normalized previews
        matches = response.get("matches", [])
        response["production_preview"] = _extract_production_preview(matches, product_type)
        response["pricing_breakdown"] = _extract_pricing_breakdown(matches)

        return _attach_booklet_match_metadata(response, request_payload)

    paper_stock_raw = payload.get("paper_stock") or ""
    stock = resolve_stock_option(paper_stock_raw, usage="sticker" if product_type == "label_sticker" else "")
    tier_gsm: int | None = None
    if stock is None and paper_stock_raw.endswith("gsm"):
        try:
            tier_gsm = int(paper_stock_raw[:-3])
        except ValueError:
            pass
    is_custom_size = finished_size_raw == "custom"
    request_payload = {
        "calculator_mode": "marketplace",
        "product_family": PRODUCT_FAMILY_BY_TYPE[product_type],
        "pricing_mode": "custom",
        "product_pricing_mode": "SHEET",
        "quantity": payload.get("quantity"),
        "size_mode": "custom" if is_custom_size else "standard",
        "size_label": None if is_custom_size else finished_size_raw,
        "width_mm": size["width_mm"],
        "height_mm": size["height_mm"],
        "sides": payload.get("print_sides") or definition["defaults"].get("print_sides"),
        "color_mode": payload.get("color_mode") or definition["defaults"].get("color_mode"),
        "paper_type": payload.get("requested_paper_category") or (stock or {}).get("category"),
        "paper_gsm": payload.get("requested_gsm") or (stock or {}).get("gsm") or tier_gsm,
        "finishing_slugs": [
            value
            for value in [
                payload.get("lamination") if payload.get("lamination") not in {None, "", "none"} else None,
                payload.get("folding") if payload.get("folding") not in {None, "", "none"} else None,
                "corner-rounding" if payload.get("corner_rounding") else None,
                payload.get("cut_type"),
            ]
            if value
        ],
        "turnaround_hours": payload.get("turnaround_hours"),
    }
    response = get_marketplace_matches(request_payload)
    response["product_type"] = product_type
    response["can_calculate"] = bool(response.get("matches_count"))
    response["price_mode"] = "exact" if response.get("exact_or_estimated") else "estimate"
    response["missing_fields"] = response.get("missing_requirements", [])
    if custom_warnings:
        response["warnings"] = list(response.get("warnings") or []) + custom_warnings

    # Attach normalized previews
    matches = response.get("matches", [])
    response["production_preview"] = _extract_production_preview(matches, product_type)
    response["pricing_breakdown"] = _extract_pricing_breakdown(matches)

    return _attach_flat_match_metadata(response, request_payload)
