from __future__ import annotations

from copy import deepcopy
from typing import Any

from inventory.models import Paper
from pricing.models import FinishingRate
from services.pricing.calculator_config import (
    get_product_definition,
    resolve_finished_size,
    resolve_stock_option,
)
from services.pricing.calculator_preview import PRODUCT_FAMILY_BY_TYPE, _parse_tier_gsm
from services.pricing.urgency import apply_priority_pricing
from services.public_matching import (
    _find_best_product_match,
    _preview_booklet_for_shop,
    _preview_custom_for_shop,
    _resolve_binding_rate_for_shop,
)
from shops.models import Shop


def _required_missing(payload: dict[str, Any], definition: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in definition["required_fields"]:
        if field == "paper_stock":
            if not (
                payload.get("paper_stock")
                or payload.get("requested_paper_category")
                or payload.get("requested_gsm")
            ):
                missing.append(field)
            continue
        if field == "cover_stock":
            if not (
                payload.get("cover_stock")
                or payload.get("requested_cover_paper_category")
                or payload.get("requested_cover_gsm")
            ):
                missing.append(field)
            continue
        if field == "insert_stock":
            if not (
                payload.get("insert_stock")
                or payload.get("requested_insert_paper_category")
                or payload.get("requested_insert_gsm")
            ):
                missing.append(field)
            continue
        value = payload.get(field)
        if value in (None, "", []):
            missing.append(field)
    return missing


def _build_missing_response(product_type: str, missing_fields: list[str]) -> dict[str, Any]:
    return {
        "product_type": product_type,
        "summary": f"Missing fields for {product_type.replace('_', ' ')} matching.",
        "missing_fields": missing_fields,
        "results": [],
        "matched_count": 0,
        "results_count": 0,
        "visibility": {
            "actor": "partner_admin",
            "exposes_internal_economics": True,
        },
    }


def _candidate_shops() -> list[Shop]:
    return list(
        Shop.objects.filter(is_active=True)
        .select_related("location")
        .order_by("name", "id")
    )


def _contains_token(value: str, tokens: tuple[str, ...]) -> bool:
    haystack = (value or "").strip().lower()
    return any(token in haystack for token in tokens)


def _has_cutting_capability(shop: Shop) -> bool:
    for finishing in FinishingRate.objects.filter(shop=shop, is_active=True).only("name", "slug"):
        if _contains_token(finishing.name, ("cut", "trim")) or _contains_token(finishing.slug, ("cut", "trim")):
            return True
    return False


def _location_summary(shop: Shop) -> str | None:
    parts = [
        getattr(shop, "service_area", "") or "",
        getattr(shop, "city", "") or "",
        getattr(getattr(shop, "location", None), "name", "") or "",
    ]
    unique_parts: list[str] = []
    for part in parts:
        normalized = str(part).strip()
        if normalized and normalized not in unique_parts:
            unique_parts.append(normalized)
    if not unique_parts:
        return None
    return ", ".join(unique_parts[:2])


def _normalize_missing_requirements(values: list[str]) -> list[str]:
    normalized: list[str] = []
    mapping = {
        "finishings": "finishing",
        "machine": "pricing",
        "paper_stock": "paper",
    }
    for value in values:
        target = mapping.get(str(value), str(value))
        if target not in normalized:
            normalized.append(target)
    return normalized


def _available_reasons(row: dict[str, Any]) -> list[str]:
    selection = row.get("selection") or {}
    production_preview = row.get("production_preview") or {}
    reasons: list[str] = []

    if selection.get("paper_label"):
        reasons.append(f"Paper available: {selection['paper_label']}")
    if selection.get("material_label"):
        reasons.append(f"Material available: {selection['material_label']}")
    if selection.get("machine_label"):
        reasons.append(f"Pricing path available on {selection['machine_label']}")
    if selection.get("cover_paper_label"):
        reasons.append(f"Cover stock available: {selection['cover_paper_label']}")
    if selection.get("insert_paper_label"):
        reasons.append(f"Inner stock available: {selection['insert_paper_label']}")
    if selection.get("binding_rate_label"):
        reasons.append(f"Binding available: {selection['binding_rate_label']}")

    finishings = production_preview.get("selected_finishings") or []
    for finishing in finishings:
        label = str(finishing).strip()
        if label:
            reasons.append(f"Finishing priced: {label}")

    if row.get("can_calculate") and row.get("total") and "Backend pricing preview available" not in reasons:
        reasons.append("Backend pricing preview available")

    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


def _override_missing_capability(
    *,
    row: dict[str, Any],
    reason: str,
    missing_requirements: list[str],
) -> dict[str, Any]:
    updated = deepcopy(row)
    merged_missing = list(updated.get("missing_fields") or [])
    for item in missing_requirements:
        if item not in merged_missing:
            merged_missing.append(item)
    updated["can_calculate"] = False
    updated["can_price_now"] = False
    updated["total"] = None
    updated["reason"] = reason
    updated["summary"] = reason
    updated["missing_fields"] = merged_missing
    updated["preview"] = None
    updated["production_preview"] = None
    updated["pricing_breakdown"] = None
    return updated


def _raw_failure_row(shop: Shop, *, reason: str, missing_fields: list[str]) -> dict[str, Any]:
    return {
        "shop_name": shop.name,
        "currency": getattr(shop, "currency", "KES") or "KES",
        "can_calculate": False,
        "reason": reason,
        "summary": reason,
        "missing_fields": missing_fields,
        "selection": {},
        "preview": None,
        "production_preview": None,
        "pricing_breakdown": None,
        "turnaround_label": "On request",
        "human_ready_text": "Ready time on request",
        "turnaround_hours": None,
        "match_type": "needs_confirmation",
    }


def _apply_minimum_inference(product_type: str, payload: dict[str, Any], shop: Shop, row: dict[str, Any]) -> dict[str, Any]:
    if product_type in {"business_card", "flyer"} and not _has_cutting_capability(shop):
        return _override_missing_capability(
            row=row,
            reason="Cutting finishing is not configured for this shop.",
            missing_requirements=["finishing", "cutting"],
        )

    if product_type == "booklet":
        binding_type = payload.get("binding_type") or "saddle_stitch"
        if _resolve_binding_rate_for_shop(shop, binding_type) is None:
            return _override_missing_capability(
                row=row,
                reason="Binding finishing is not configured for this shop.",
                missing_requirements=["binding", "finishing"],
            )

    return row


def _normalize_result_row(product_type: str, shop: Shop, row: dict[str, Any]) -> dict[str, Any]:
    missing_requirements = _normalize_missing_requirements(list(row.get("missing_fields") or []))
    price_available = bool(row.get("can_calculate") and row.get("total") not in (None, ""))
    missing_specs = {"paper", "finished_size", "quantity", "print_sides", "color_mode", "finishing", "width_mm", "height_mm", "material", "total_pages", "cover_stock", "insert_stock", "binding"}

    if price_available and row.get("can_calculate"):
        price_status = "priced"
    elif any(item in missing_specs for item in missing_requirements):
        price_status = "missing_specs"
    elif "pricing" in missing_requirements:
        price_status = "missing_pricing"
    else:
        price_status = "missing_capability"

    estimated_turnaround = row.get("human_ready_text") or row.get("turnaround_label")
    location = _location_summary(shop)
    explanation = row.get("reason") or row.get("summary") or ""

    return {
        "shop_id": shop.id,
        "shop_display_name": row.get("shop_name") or shop.name,
        "shop_slug": getattr(shop, "slug", "") or "",
        "can_produce": bool(row.get("can_calculate")),
        "production_cost": row.get("total") if price_available else None,
        "currency": row.get("currency") or getattr(shop, "currency", "KES") or "KES",
        "price_available": price_available,
        "price_status": price_status,
        "missing_requirements": missing_requirements,
        "available_reasons": _available_reasons(row),
        "estimated_turnaround": estimated_turnaround,
        "turnaround_hours": row.get("turnaround_hours"),
        "turnaround_label": row.get("turnaround_label"),
        "location_summary": location,
        "location_area": location,
        "match_type": row.get("match_type"),
        "match_score": row.get("match_score") or row.get("similarity_score"),
        "reason": explanation,
        "explanation": explanation,
        "product_type": product_type,
        "preview_snapshot": row.get("preview"),
        "selection": row.get("selection") or {},
    }


def _sort_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_order = {
        "priced": 0,
        "missing_pricing": 1,
        "missing_specs": 2,
        "missing_capability": 3,
    }
    return sorted(
        results,
        key=lambda row: (
            status_order.get(str(row.get("price_status") or ""), 99),
            float(row["production_cost"]) if row["production_cost"] not in (None, "") else 999999999.0,
            int(row.get("turnaround_hours") or 999999),
            len(row.get("missing_requirements") or []),
            -float(row.get("match_score") or 0),
            row["shop_display_name"],
        ),
    )


def _apply_recommendations(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priced_rows = [row for row in results if row.get("price_status") == "priced"]
    fastest_row = None
    if priced_rows:
        fastest_row = min(
            priced_rows,
            key=lambda row: (int(row.get("turnaround_hours") or 999999), float(row.get("production_cost") or 999999999.0)),
        )

    updated: list[dict[str, Any]] = []
    for index, row in enumerate(results, start=1):
        next_row = deepcopy(row)
        next_row["recommendation_rank"] = index
        if index == 1 and row.get("price_status") == "priced":
            next_row["recommendation_label"] = "Recommended"
        elif fastest_row and row.get("shop_id") == fastest_row.get("shop_id") and row.get("price_status") == "priced":
            next_row["recommendation_label"] = "Fastest"
        elif row.get("price_status") == "priced":
            next_row["recommendation_label"] = "Cheapest"
        elif row.get("price_status") == "missing_pricing":
            next_row["recommendation_label"] = "Needs setup"
        elif row.get("price_status") == "missing_specs":
            next_row["recommendation_label"] = "Missing specs"
        else:
            next_row["recommendation_label"] = "Needs setup"
        updated.append(next_row)
    return updated


def _build_pricing_snapshot(results: list[dict[str, Any]]) -> dict[str, Any]:
    selected_shops = []
    numeric_prices: list[float] = []
    currency = "KES"
    for row in results:
        if row.get("currency"):
            currency = row["currency"]
        if row.get("production_cost") not in (None, ""):
            try:
                numeric_prices.append(float(row["production_cost"]))
            except (TypeError, ValueError):
                pass
        selected_shops.append(
            {
                "id": row["shop_id"],
                "shop_id": row["shop_id"],
                "name": row["shop_display_name"],
                "shop_name": row["shop_display_name"],
                "slug": row.get("shop_slug") or "",
                "shop_slug": row.get("shop_slug") or "",
                "can_calculate": row["can_produce"],
                "can_price_now": row["price_available"],
                "currency": row.get("currency") or currency,
                "reason": row.get("reason") or "",
                "summary": row.get("reason") or "",
                "missing_fields": row.get("missing_requirements") or [],
                "preview": row.get("preview_snapshot"),
                "selection": row.get("selection") or {},
                "turnaround_label": row.get("turnaround_label"),
                "turnaround_hours": row.get("turnaround_hours"),
                "human_ready_text": row.get("estimated_turnaround"),
                "distance_label": row.get("location_summary"),
                "estimated_price": row.get("production_cost"),
            }
        )
    return {
        "selected_shops": selected_shops,
        "matches": selected_shops,
        "shops": selected_shops,
        "currency": currency,
        "min_price": f"{min(numeric_prices):.2f}" if numeric_prices else None,
        "max_price": f"{max(numeric_prices):.2f}" if numeric_prices else None,
        "summary": (
            f"Found {sum(1 for row in results if row['can_produce'])} production-capable shop match(es)."
            if results else "No production shop matches yet."
        ),
    }


def _build_flat_payload(payload: dict[str, Any], product_type: str, definition: dict[str, Any]) -> dict[str, Any]:
    finished_size_raw = payload.get("finished_size") or ""
    if finished_size_raw == "custom":
        width_mm = payload.get("width_mm")
        height_mm = payload.get("height_mm")
        if not width_mm or not height_mm:
            raise ValueError("custom_size_missing")
        size = {"width_mm": width_mm, "height_mm": height_mm}
    else:
        size = resolve_finished_size(product_type, finished_size_raw)
        if not size:
            raise ValueError("finished_size_missing")

    stock = resolve_stock_option(
        payload.get("paper_stock") or "",
        usage="sticker" if product_type == "label_sticker" else "",
    )
    tier_gsm: int | None = None
    paper_stock_raw = payload.get("paper_stock") or ""
    if stock is None and isinstance(paper_stock_raw, str) and paper_stock_raw.endswith("gsm"):
        tier_gsm = _parse_tier_gsm(paper_stock_raw)

    request_payload = {
        "calculator_mode": "partner_matcher",
        "product_family": PRODUCT_FAMILY_BY_TYPE[product_type],
        "pricing_mode": "custom",
        "product_pricing_mode": "SHEET",
        "quantity": payload.get("quantity"),
        "size_mode": "custom" if finished_size_raw == "custom" else "standard",
        "size_label": None if finished_size_raw == "custom" else finished_size_raw,
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
        "urgency_type": payload.get("urgency_type"),
        "requested_deadline": payload.get("requested_deadline"),
        "requested_delivery_time": payload.get("requested_delivery_time"),
    }
    return request_payload


def _build_booklet_payload(payload: dict[str, Any], definition: dict[str, Any]) -> dict[str, Any]:
    cover_stock = resolve_stock_option(payload.get("cover_stock") or "", usage="cover")
    insert_stock = resolve_stock_option(payload.get("insert_stock") or "", usage="insert")
    cover_tier_gsm = _parse_tier_gsm(payload.get("cover_stock")) if not cover_stock else None
    insert_tier_gsm = _parse_tier_gsm(payload.get("insert_stock")) if not insert_stock else None
    size = resolve_finished_size("booklet", payload.get("finished_size") or "")
    if not size:
        raise ValueError("finished_size_missing")

    return {
        "product_family": "booklet",
        "quantity": payload.get("quantity"),
        "total_pages": payload.get("total_pages"),
        "binding_type": payload.get("binding_type") or definition["defaults"].get("binding_type"),
        "cover_paper_type": payload.get("requested_cover_paper_category") or (cover_stock or {}).get("category"),
        "cover_paper_gsm": payload.get("requested_cover_gsm") or (cover_stock or {}).get("gsm") or cover_tier_gsm,
        "insert_paper_type": payload.get("requested_insert_paper_category") or (insert_stock or {}).get("category"),
        "insert_paper_gsm": payload.get("requested_insert_gsm") or (insert_stock or {}).get("gsm") or insert_tier_gsm,
        "cover_lamination_mode": payload.get("cover_lamination") or definition["defaults"].get("cover_lamination"),
        "cover_sides": "DUPLEX",
        "insert_sides": "DUPLEX",
        "cover_color_mode": payload.get("color_mode") or "COLOR",
        "insert_color_mode": payload.get("color_mode") or "COLOR",
        "width_mm": size["width_mm"],
        "height_mm": size["height_mm"],
        "turnaround_hours": payload.get("turnaround_hours"),
        "urgency_type": payload.get("urgency_type"),
        "requested_deadline": payload.get("requested_deadline"),
        "requested_delivery_time": payload.get("requested_delivery_time"),
    }


def _build_large_format_payload(payload: dict[str, Any], definition: dict[str, Any]) -> dict[str, Any]:
    width_mm = payload.get("width_mm")
    height_mm = payload.get("height_mm")
    if not width_mm or not height_mm:
        raise ValueError("custom_size_missing")

    return {
        "calculator_mode": "partner_matcher",
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
        "urgency_type": payload.get("urgency_type"),
        "requested_deadline": payload.get("requested_deadline"),
        "requested_delivery_time": payload.get("requested_delivery_time"),
    }


def _build_request_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    product_type = str(payload.get("product_type") or "").strip()
    definition = get_product_definition(product_type)
    if not definition:
        raise KeyError("product_type")

    missing = _required_missing(payload, definition)
    if missing:
        raise ValueError(",".join(missing))

    if product_type == "booklet":
        return product_type, _build_booklet_payload(payload, definition)
    if product_type == "large_format":
        return product_type, _build_large_format_payload(payload, definition)
    return product_type, _build_flat_payload(payload, product_type, definition)


def build_partner_production_matches(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        product_type, request_payload = _build_request_payload(payload)
    except KeyError:
        return _build_missing_response(str(payload.get("product_type") or ""), ["product_type"])
    except ValueError as exc:
        missing_fields = [value for value in str(exc).split(",") if value]
        return _build_missing_response(str(payload.get("product_type") or ""), missing_fields)

    results: list[dict[str, Any]] = []
    for shop in _candidate_shops():
        if product_type != "booklet" and request_payload.get("product_pricing_mode") == "SHEET":
            has_priced_paper = Paper.objects.filter(shop=shop, is_active=True, selling_price__gt=0).exists()
            if not has_priced_paper:
                raw_row = _raw_failure_row(
                    shop,
                    reason="No compatible paper is priced for this shop.",
                    missing_fields=["paper"],
                )
            else:
                raw_row = _preview_custom_for_shop(
                    shop,
                    request_payload,
                    product_match=_find_best_product_match(shop, request_payload),
                )
        elif product_type == "booklet":
            raw_row = _preview_booklet_for_shop(shop, request_payload)
        else:
            raw_row = _preview_custom_for_shop(
                shop,
                request_payload,
                product_match=_find_best_product_match(shop, request_payload),
            )

        raw_row = _apply_minimum_inference(product_type, request_payload, shop, raw_row)

        preview = raw_row.get("preview")
        if isinstance(preview, dict) and raw_row.get("can_calculate"):
            raw_row["preview"] = apply_priority_pricing(
                preview,
                urgency_type=request_payload.get("urgency_type"),
                turnaround_hours=request_payload.get("turnaround_hours"),
                turnaround_label=preview.get("turnaround_label"),
                requested_deadline=request_payload.get("requested_deadline"),
                requested_delivery_time=request_payload.get("requested_delivery_time"),
            )

        results.append(_normalize_result_row(product_type, shop, raw_row))

    sorted_results = _apply_recommendations(_sort_results(results))
    matched_count = sum(1 for row in sorted_results if row["can_produce"])
    return {
        "product_type": product_type,
        "summary": (
            f"Found {matched_count} production-capable shop match(es)."
            if matched_count
            else "No production shops can fully price this job yet."
        ),
        "missing_fields": [],
        "results": sorted_results,
        "matched_count": matched_count,
        "results_count": len(sorted_results),
        "pricing_snapshot": _build_pricing_snapshot(sorted_results),
        "spec_snapshot": request_payload,
        "visibility": {
            "actor": "partner_admin",
            "exposes_internal_economics": True,
        },
    }
