from __future__ import annotations

from decimal import Decimal
from typing import Any

from catalog.choices import PricingMode, ProductKind
from catalog.models import Product
from common.geo import haversine_km
from inventory.models import Machine, Paper
from locations.models import Location
from pricing.models import FinishingRate, Material, PrintingRate
from services.pricing.engine import calculate_sheet_pricing
from services.pricing.large_format import calculate_large_format_preview
from quotes.turnaround import derive_product_turnaround_hours, estimate_turnaround, humanize_working_hours
from shops.models import Shop


MAX_MARKETPLACE_MATCHES = 3


def recompute_shop_match_readiness(shop: Shop) -> bool:
    has_sheet_path = (
        Machine.objects.filter(shop=shop, is_active=True).exists()
        and Paper.objects.filter(shop=shop, is_active=True, selling_price__gt=0).exists()
        and PrintingRate.objects.filter(machine__shop=shop, is_active=True).exists()
    )
    has_large_format_path = Material.objects.filter(shop=shop, is_active=True, selling_price__gt=0).exists()
    has_catalog_products = Product.objects.filter(shop=shop, is_active=True, is_public=True).exists()

    pricing_ready = has_sheet_path or has_large_format_path
    supports_catalog_requests = bool(has_catalog_products and pricing_ready)
    supports_custom_requests = bool(pricing_ready)
    public_match_ready = bool(shop.is_public and shop.is_active and pricing_ready and (supports_catalog_requests or supports_custom_requests))

    Shop.objects.filter(pk=shop.pk).update(
        pricing_ready=pricing_ready,
        public_match_ready=public_match_ready,
        supports_catalog_requests=supports_catalog_requests,
        supports_custom_requests=supports_custom_requests,
    )

    shop.pricing_ready = pricing_ready
    shop.public_match_ready = public_match_ready
    shop.supports_catalog_requests = supports_catalog_requests
    shop.supports_custom_requests = supports_custom_requests
    return pricing_ready


def _requested_family(payload: dict[str, Any]) -> str:
    family = payload.get("product_family")
    if family in {"flat", "booklet", "large_format"}:
        return family
    if payload.get("product_pricing_mode") == "LARGE_FORMAT":
        return "large_format"
    return "flat"


def _distance_km_for_payload(shop: Shop, payload: dict[str, Any]) -> float | None:
    lat = payload.get("lat")
    lng = payload.get("lng")
    if lat is None or lng is None or shop.latitude is None or shop.longitude is None:
        return None
    return round(haversine_km(float(lat), float(lng), float(shop.latitude), float(shop.longitude)), 2)


def _apply_radius_filter(queryset, payload: dict[str, Any]):
    lat = payload.get("lat")
    lng = payload.get("lng")
    location_slug = payload.get("location_slug")
    
    if location_slug:
        loc = Location.objects.filter(slug=location_slug, is_active=True).first()
        if loc and loc.latitude and loc.longitude:
            lat = float(loc.latitude)
            lng = float(loc.longitude)

    radius = payload.get("radius_km") or 50
    if lat is None or lng is None:
        return queryset

    # Update payload for distance calculations in Try functions
    payload["lat"] = lat
    payload["lng"] = lng

    shop_ids = []
    for shop in queryset:
        distance = _distance_km_for_payload(shop, payload)
        if distance is not None and distance <= radius:
            shop_ids.append(shop.id)
    return queryset.filter(id__in=shop_ids)


def _family_capability_queryset(queryset, family: str):
    if family == "large_format":
        return queryset.filter(
            materials__is_active=True,
            materials__selling_price__gt=0,
        )
    if family == "booklet":
        return queryset.filter(
            machines__is_active=True,
            papers__is_active=True,
            papers__selling_price__gt=0,
            machines__printing_rates__is_active=True,
        )
    return queryset.filter(
        machines__is_active=True,
        papers__is_active=True,
        papers__selling_price__gt=0,
        machines__printing_rates__is_active=True,
    )


def _product_availability_score(shop: Shop, family: str) -> float:
    # Product match is now a bonus, not a requirement.
    public_products = Product.objects.filter(shop=shop, is_active=True, is_public=True)
    if family == "large_format":
        exists = public_products.filter(pricing_mode=PricingMode.LARGE_FORMAT).exists()
    elif family == "booklet":
        exists = public_products.filter(pricing_mode=PricingMode.SHEET, product_kind=ProductKind.BOOKLET).exists()
    else:
        exists = public_products.filter(pricing_mode=PricingMode.SHEET, product_kind=ProductKind.FLAT).exists()
    return 5.0 if exists else 0.0


def _pricing_readiness_score(shop: Shop) -> float:
    score = 0.0
    if getattr(shop, "public_match_ready", False):
        score += 20.0
    if getattr(shop, "supports_custom_requests", False):
        score += 10.0
    if getattr(shop, "supports_catalog_requests", False):
        score += 5.0
    return score


def _sort_match_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -float(row.get("confidence_score") or row.get("similarity_score") or 0.0),
            0 if row.get("can_calculate") else 1,
            _as_decimal(row.get("total")) if row.get("can_calculate") and row.get("total") else Decimal("999999999.99"),
            row["name"],
        ),
    )


def _build_marketplace_response(*, successful_rows: list[dict[str, Any]], failed_rows: list[dict[str, Any]], mode: str = "marketplace") -> dict[str, Any]:
    selected_rows = _sort_match_rows(successful_rows)[:MAX_MARKETPLACE_MATCHES]
    totals = [_as_decimal(row.get("total")) for row in successful_rows if row.get("total")]
    missing_requirements = _unique_strings(field for row in failed_rows for field in row.get("missing_fields", []))
    unsupported_reasons = _unique_strings(row.get("reason") for row in failed_rows if row.get("reason"))

    best_match = selected_rows[0] if selected_rows else (failed_rows[0] if failed_rows else None)

    return {
        "mode": mode,
        "can_calculate": bool(successful_rows),
        "matches_count": len(successful_rows),
        "matches": selected_rows,
        "shops": selected_rows,
        "selected_shops": selected_rows,
        "min_price": _format_decimal(min(totals)) if totals else None,
        "max_price": _format_decimal(max(totals)) if totals else None,
        "currency": selected_rows[0]["currency"] if selected_rows else "KES",
        "production_preview": best_match.get("production_preview") if best_match else None,
        "pricing_breakdown": best_match.get("pricing_breakdown") if best_match else None,
        "missing_requirements": missing_requirements,
        "unsupported_reasons": unsupported_reasons,
        "summary": _build_marketplace_summary(successful_rows, failed_rows),
        "suggestions": ["Detailed shop-spec matching is not fully connected yet."] if not successful_rows else [],
        "exact_or_estimated": bool(selected_rows) and all(row.get("exact_or_estimated", False) for row in selected_rows),
    }


def get_marketplace_matches(payload: dict[str, Any]) -> dict[str, Any]:
    candidate_shops = list(filter_candidate_shops(payload))
    rows = [try_preview_for_shop(shop, payload) for shop in candidate_shops]
    rows = [row for row in rows if row is not None]

    successful_rows = [row for row in rows if row["can_calculate"]]
    failed_rows = [row for row in rows if not row["can_calculate"]]
    return _build_marketplace_response(successful_rows=successful_rows, failed_rows=failed_rows)


def get_shop_specific_preview(shop: Shop, payload: dict[str, Any]) -> dict[str, Any]:
    row = try_preview_for_shop(shop, payload)
    selected_rows = [row] if row and row["can_calculate"] else []
    return {
        "mode": "single-shop",
        "can_calculate": row["can_calculate"] if row else False,
        "matches_count": 1 if row and row["can_calculate"] else 0,
        "matches": selected_rows,
        "shops": selected_rows,
        "selected_shops": selected_rows,
        "fixed_shop_preview": row,
        "production_preview": row.get("production_preview") if row else None,
        "pricing_breakdown": row.get("pricing_breakdown") if row else None,
        "min_price": row.get("total") if row and row["can_calculate"] else None,
        "max_price": row.get("total") if row and row["can_calculate"] else None,
        "currency": row.get("currency", getattr(shop, "currency", "KES")) if row else getattr(shop, "currency", "KES"),
        "missing_requirements": row.get("missing_fields", []) if row else [],
        "unsupported_reasons": [row["reason"]] if row and row.get("reason") and not row["can_calculate"] else [],
        "summary": row["reason"] if row and row.get("reason") else ("Preview ready." if row and row["can_calculate"] else "Preview unavailable."),
        "suggestions": [],
        "exact_or_estimated": bool(row and row.get("exact_or_estimated")),
    }


def filter_candidate_shops(payload: dict[str, Any]):
    queryset = Shop.objects.filter(public_match_ready=True, is_active=True, is_public=True)
    family = _requested_family(payload)

    if payload.get("pricing_mode") == "catalog":
        queryset = queryset.filter(supports_catalog_requests=True)
    else:
        queryset = queryset.filter(supports_custom_requests=True)
        # We no longer strictly filter by product existence here.
        # _family_capability_queryset will check for machines/papers/materials.
        queryset = _family_capability_queryset(queryset, family)

    queryset = _apply_radius_filter(queryset.distinct(), payload)
    return queryset.distinct()


def try_preview_for_shop(shop: Shop, payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("pricing_mode") == "catalog":
        product_id = payload.get("product_id")
        product_slug = payload.get("product_slug")
        if not product_id and not product_slug:
            return None
        
        queryset = Product.objects.filter(shop=shop, is_active=True, is_public=True)
        if product_id:
            product = queryset.filter(id=product_id).first()
        else:
            product = queryset.filter(slug=product_slug).first()
            
        if not product:
            return None
        return _preview_catalog_for_shop(shop, product, payload)
    
    # Check if shop has a matching product for this custom job to include it as a bonus match
    product_match = _find_best_product_match(shop, payload)
    return _preview_custom_for_shop(shop, payload, product_match=product_match)


def _find_best_product_match(shop: Shop, payload: dict[str, Any]) -> Product | None:
    family = _requested_family(payload)
    products = Product.objects.filter(shop=shop, is_active=True, is_public=True)
    if family == "large_format":
        products = products.filter(pricing_mode=PricingMode.LARGE_FORMAT)
    elif family == "booklet":
        products = products.filter(pricing_mode=PricingMode.SHEET, product_kind=ProductKind.BOOKLET)
    else:
        products = products.filter(pricing_mode=PricingMode.SHEET, product_kind=ProductKind.FLAT)
    
    # Simple match by slug/title if available, or just return first active product of family
    return products.first()


def _turnaround_hours_for_payload(shop: Shop, payload: dict[str, Any], product: Product | None = None) -> int | None:
    if product is not None:
        return derive_product_turnaround_hours(
            product,
            rush=(payload.get("turnaround_mode") == "rush"),
        )
    if payload.get("turnaround_hours"):
        return int(payload["turnaround_hours"])
    if payload.get("turnaround_days"):
        return int(payload["turnaround_days"]) * 8
    return None


def _attach_turnaround(shop: Shop, row: dict[str, Any], payload: dict[str, Any], product: Product | None = None) -> dict[str, Any]:
    turnaround_hours = _turnaround_hours_for_payload(shop, payload, product=product)
    estimate = estimate_turnaround(shop=shop, working_hours=turnaround_hours)
    row["distance_km"] = row.get("distance_km", _distance_km_for_payload(shop, payload))
    row["turnaround_hours"] = turnaround_hours
    row["estimated_working_hours"] = turnaround_hours
    row["estimated_ready_at"] = estimate.ready_at if estimate else None
    row["human_ready_text"] = estimate.human_ready_text if estimate else "Ready time on request"
    row["turnaround_label"] = estimate.label if estimate else "On request"
    row_preview = row.get("preview") or {}
    row_preview["turnaround_hours"] = turnaround_hours
    row_preview["estimated_working_hours"] = turnaround_hours
    row_preview["estimated_ready_at"] = estimate.ready_at if estimate else None
    row_preview["human_ready_text"] = estimate.human_ready_text if estimate else "Ready time on request"
    row_preview["turnaround_label"] = estimate.label if estimate else "On request"
    row_preview["turnaround_text"] = humanize_working_hours(turnaround_hours)
    row["preview"] = row_preview
    return row


def _preview_catalog_for_shop(shop: Shop, product: Product, payload: dict[str, Any]) -> dict[str, Any]:
    finishing_selections, missing_finishings = _resolve_finishings(shop, payload)
    product_pricing_mode = product.pricing_mode

    if product_pricing_mode == "LARGE_FORMAT":
        material = _pick_material(shop, payload)
        if not material:
            return _build_shop_row(
                shop,
                can_calculate=False,
                reason="No compatible material is priced for this shop.",
                missing_fields=["material", *missing_finishings],
                similarity_score=_shop_similarity_score(shop, payload, False),
                quote_basis="insufficient_data",
                payload=payload,
            )

        width_mm = int(payload.get("width_mm") or product.default_finished_width_mm or 0)
        height_mm = int(payload.get("height_mm") or product.default_finished_height_mm or 0)
        if not width_mm or not height_mm:
            return _build_shop_row(
                shop,
                can_calculate=False,
                reason="Finished size is required for large-format pricing.",
                missing_fields=["width_mm", "height_mm", *missing_finishings],
                similarity_score=_shop_similarity_score(shop, payload, False),
                quote_basis="insufficient_data",
                payload=payload,
            )

        result = calculate_large_format_preview(
            shop=shop,
            product_subtype=payload.get("product_subtype") or "banner",
            quantity=payload["quantity"],
            width_mm=width_mm,
            height_mm=height_mm,
            material=material,
            finishing_selections=finishing_selections,
            turnaround_hours=payload.get("turnaround_hours"),
        )
        return _attach_turnaround(shop, _build_shop_row(
            shop,
            can_calculate=True,
            total=result["totals"]["grand_total"],
            currency=result["currency"],
            reason="Exact preview from this shop.",
            preview=result,
            selection={"material_id": material.id, "material_label": f"{material.material_type} ({material.unit})"},
            similarity_score=_shop_similarity_score(shop, payload, True, material=material),
            exact_or_estimated=True,
            quote_basis="product_and_rate_card",
            product_match=product,
            payload=payload,
        ), payload, product)

    paper = _pick_paper(shop, payload, product=product)
    if not paper:
        return _build_shop_row(
            shop,
            can_calculate=False,
            reason="No compatible paper is priced for this shop.",
            missing_fields=["paper", *missing_finishings],
            similarity_score=_shop_similarity_score(shop, payload, False),
            quote_basis="insufficient_data",
            payload=payload,
        )

    machine = _pick_machine(shop, paper=paper, payload=payload, product=product)
    if not machine:
        return _build_shop_row(
            shop,
            can_calculate=False,
            reason="No compatible print-rate path is ready for this shop.",
            missing_fields=["machine", *missing_finishings],
            selection={"paper_id": paper.id, "paper_label": _paper_label(paper)},
            similarity_score=_shop_similarity_score(shop, payload, False, paper=paper),
            quote_basis="insufficient_data",
            payload=payload,
        )

    result = calculate_sheet_pricing(
        shop=shop,
        product=product,
        quantity=payload["quantity"],
        paper=paper,
        machine=machine,
        color_mode=payload.get("color_mode") or payload.get("colour_mode") or "COLOR",
        sides=payload.get("sides") or payload.get("print_sides") or "SIMPLEX",
        apply_duplex_surcharge=payload.get("apply_duplex_surcharge"),
        finishing_selections=finishing_selections,
        width_mm=int(payload.get("width_mm") or product.default_finished_width_mm or 0),
        height_mm=int(payload.get("height_mm") or product.default_finished_height_mm or 0),
    ).to_dict()

    return _attach_turnaround(shop, _build_shop_row(
        shop,
        can_calculate=True,
        total=result["totals"]["grand_total"],
        currency=result["currency"],
        reason="Exact preview from this shop.",
        preview=result,
        selection={
            "paper_id": paper.id,
            "paper_label": _paper_label(paper),
            "machine_id": machine.id,
            "machine_label": getattr(machine, "name", ""),
        },
        similarity_score=_shop_similarity_score(shop, payload, True, paper=paper),
        exact_or_estimated=True,
        quote_basis="product_and_rate_card",
        product_match=product,
        payload=payload,
    ), payload, product)


def _preview_custom_for_shop(shop: Shop, payload: dict[str, Any], product_match: Product | None = None) -> dict[str, Any]:
    finishing_selections, missing_finishings = _resolve_finishings(shop, payload)
    product_pricing_mode = payload.get("product_pricing_mode") or _infer_product_pricing_mode(payload)

    if product_pricing_mode == "LARGE_FORMAT":
        material = _pick_material(shop, payload)
        width_mm = int(payload.get("width_mm") or 0)
        height_mm = int(payload.get("height_mm") or 0)
        missing_fields = list(missing_finishings)
        if not material:
            missing_fields.append("material")
        if not width_mm:
            missing_fields.append("width_mm")
        if not height_mm:
            missing_fields.append("height_mm")
        if missing_fields:
            return _build_shop_row(
                shop,
                can_calculate=False,
                reason="Add material and finished size to price this custom job.",
                missing_fields=missing_fields,
                selection={"material_id": material.id, "material_label": f"{material.material_type} ({material.unit})"} if material else {},
                similarity_score=_shop_similarity_score(shop, payload, False, material=material),
                quote_basis="manual_quote" if not material else "insufficient_data",
                product_match=product_match,
                payload=payload,
            )

        result = calculate_large_format_preview(
            shop=shop,
            product_subtype=payload.get("product_subtype") or "banner",
            quantity=payload["quantity"],
            width_mm=width_mm,
            height_mm=height_mm,
            material=material,
            finishing_selections=finishing_selections,
            turnaround_hours=payload.get("turnaround_hours"),
        )
        return _attach_turnaround(shop, _build_shop_row(
            shop,
            can_calculate=True,
            total=result["totals"]["grand_total"],
            currency=result["currency"],
            reason="Exact custom-spec preview from this shop.",
            preview=result,
            selection={"material_id": material.id, "material_label": f"{material.material_type} ({material.unit})"},
            similarity_score=_shop_similarity_score(shop, payload, True, material=material),
            exact_or_estimated=True,
            quote_basis="rate_card",
            product_match=product_match,
            payload=payload,
        ), payload, None)

    paper = _pick_paper(shop, payload)
    machine = _pick_machine(shop, paper=paper, payload=payload) if paper else None
    width_mm = int(payload.get("width_mm") or 0)
    height_mm = int(payload.get("height_mm") or 0)
    missing_fields = list(missing_finishings)
    if not width_mm:
        missing_fields.append("width_mm")
    if not height_mm:
        missing_fields.append("height_mm")
    if not paper:
        missing_fields.append("paper")
    if paper and not machine:
        missing_fields.append("machine")

        if missing_fields:
            selection = {}
            if paper:
                selection["paper_id"] = paper.id
                selection["paper_label"] = _paper_label(paper)
            if machine:
                selection["machine_id"] = machine.id
                selection["machine_label"] = getattr(machine, "name", "")
            return _build_shop_row(
                shop,
                can_calculate=False,
                reason="Add paper and finished size to price this custom job exactly.",
                missing_fields=missing_fields,
                selection=selection,
                similarity_score=_shop_similarity_score(shop, payload, False, paper=paper),
                quote_basis="manual_quote" if not paper else "insufficient_data",
                product_match=product_match,
                payload=payload,
            )

    result = calculate_sheet_pricing(
        shop=shop,
        product=None,
        quantity=payload["quantity"],
        paper=paper,
        machine=machine,
        color_mode=payload.get("color_mode") or payload.get("colour_mode") or "COLOR",
        sides=payload.get("sides") or payload.get("print_sides") or "SIMPLEX",
        apply_duplex_surcharge=payload.get("apply_duplex_surcharge"),
        finishing_selections=finishing_selections,
        width_mm=width_mm,
        height_mm=height_mm,
    ).to_dict()

    return _attach_turnaround(shop, _build_shop_row(
        shop,
        can_calculate=True,
        total=result["totals"]["grand_total"],
        currency=result["currency"],
        reason="Exact custom-spec preview from this shop.",
        preview=result,
        selection={
            "paper_id": paper.id,
            "paper_label": _paper_label(paper),
            "machine_id": machine.id,
            "machine_label": getattr(machine, "name", ""),
        },
        similarity_score=_shop_similarity_score(shop, payload, True, paper=paper),
        exact_or_estimated=True,
        quote_basis="rate_card",
        product_match=product_match,
        payload=payload,
    ), payload, None)


def _resolve_finishings(shop: Shop, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    finishing_selections = payload.get("finishing_selections") or []
    finishing_ids = payload.get("finishing_ids") or []
    finishing_slugs = payload.get("finishing_slugs") or []

    if not finishing_selections and not finishing_ids and not finishing_slugs:
        return [], []

    selections: list[dict[str, Any]] = []
    missing: list[str] = []

    if finishing_ids:
        rates = list(FinishingRate.objects.filter(shop=shop, pk__in=finishing_ids, is_active=True))
        by_id = {rate.id: rate for rate in rates}
        for finishing_id in finishing_ids:
            rate = by_id.get(finishing_id)
            if rate:
                side = _selected_side_for_finishing(finishing_id, None, finishing_selections)
                selections.append({"rule": rate, "selected_side": side})
            else:
                missing.append("finishings")

    if finishing_slugs:
        rates = list(FinishingRate.objects.filter(shop=shop, slug__in=finishing_slugs, is_active=True))
        by_slug = {rate.slug: rate for rate in rates}
        existing_ids = {selection["rule"].id for selection in selections}
        for slug in finishing_slugs:
            rate = by_slug.get(slug)
            if rate and rate.id not in existing_ids:
                side = _selected_side_for_finishing(None, slug, finishing_selections)
                selections.append({"rule": rate, "selected_side": side})
            elif not rate:
                missing.append("finishings")

    return selections, _unique_strings(missing)


def _selected_side_for_finishing(finishing_id: int | None, slug: str | None, finishing_selections: list[dict[str, Any]]) -> str:
    for selection in finishing_selections:
        if finishing_id and selection.get("finishing_id") == finishing_id:
            return selection.get("selected_side", "both")
        if slug and selection.get("slug") == slug:
            return selection.get("selected_side", "both")
    return "both"


def _pick_paper(shop: Shop, payload: dict[str, Any], product: Product | None = None) -> Paper | None:
    queryset = Paper.objects.filter(shop=shop, is_active=True, selling_price__gt=0)

    explicit_paper_id = payload.get("paper_id")
    if explicit_paper_id:
        return queryset.filter(id=explicit_paper_id).first()

    requested_gsm = payload.get("paper_gsm")
    requested_type = (payload.get("paper_type") or "").strip().lower()
    requested_sheet_size = (payload.get("sheet_size") or getattr(product, "default_sheet_size", "") or "").strip().upper()

    papers = list(queryset)
    if not papers:
        return None

    def score(paper: Paper) -> tuple[int, Decimal, int]:
        score_value = 0
        if requested_sheet_size and (paper.sheet_size or "").upper() == requested_sheet_size:
            score_value += 40
        if product and product.allowed_sheet_sizes and paper.sheet_size in product.allowed_sheet_sizes:
            score_value += 25
        if requested_gsm and paper.gsm == requested_gsm:
            score_value += 35
        elif requested_gsm:
            score_value += max(0, 20 - abs(paper.gsm - requested_gsm))
        if product and product.min_gsm and paper.gsm >= product.min_gsm:
            score_value += 5
        if product and product.max_gsm and paper.gsm <= product.max_gsm:
            score_value += 5
        paper_type = (paper.get_paper_type_display() or paper.paper_type or "").strip().lower()
        if requested_type and requested_type in paper_type:
            score_value += 25
        if product and product.default_sheet_size and (paper.sheet_size or "").upper() == product.default_sheet_size.upper():
            score_value += 10
        return (score_value, -Decimal(str(paper.selling_price)), -paper.id)

    return sorted(papers, key=score, reverse=True)[0]


def _pick_material(shop: Shop, payload: dict[str, Any]) -> Material | None:
    queryset = Material.objects.filter(shop=shop, is_active=True, selling_price__gt=0)
    material_id = payload.get("material_id")
    if material_id:
        return queryset.filter(id=material_id).first()
    requested_type = (payload.get("material_type") or "").strip().lower()
    materials = list(queryset)
    if not materials:
        return None

    def score(material: Material) -> tuple[int, Decimal, int]:
        score_value = 0
        material_type = (material.material_type or "").strip().lower()
        if requested_type and requested_type in material_type:
            score_value += 40
        return (score_value, -Decimal(str(material.selling_price)), -material.id)

    return sorted(materials, key=score, reverse=True)[0]


def _pick_machine(shop: Shop, paper: Paper | None, payload: dict[str, Any], product: Product | None = None) -> Machine | None:
    if paper is None:
        return None
    colour_mode = payload.get("color_mode") or payload.get("colour_mode") or "COLOR"
    print_sides = payload.get("sides") or payload.get("print_sides") or getattr(product, "default_sides", "SIMPLEX") or "SIMPLEX"

    queryset = (
        Machine.objects.filter(shop=shop, is_active=True)
        .filter(
            printing_rates__sheet_size=paper.sheet_size,
            printing_rates__color_mode=colour_mode,
            printing_rates__is_active=True,
        )
        .distinct()
    )

    if product and getattr(product, "default_machine_id", None):
        preferred = queryset.filter(id=product.default_machine_id).first()
        if preferred:
            resolved_rate, resolved_price = PrintingRate.resolve(preferred, paper.sheet_size, colour_mode, print_sides, paper=paper)
            if resolved_rate and resolved_price is not None:
                return preferred

    for machine in queryset.order_by("id"):
        resolved_rate, resolved_price = PrintingRate.resolve(machine, paper.sheet_size, colour_mode, print_sides, paper=paper)
        if resolved_rate and resolved_price is not None:
            return machine
    return None


def _shop_similarity_score(
    shop: Shop,
    payload: dict[str, Any],
    can_calculate: bool,
    *,
    paper: Paper | None = None,
    material: Material | None = None,
) -> float:
    score = 0.0
    family = _requested_family(payload)
    
    # same product/category: +30
    score += _product_availability_score(shop, family) * 2  # Product score is 5 or 0, so *6 to get ~30 bonus if exists
    if can_calculate:
        score += 30.0 # Base for being able to fulfill this family
    
    # paper/material exact/similar: +25
    if paper:
        requested_gsm = payload.get("paper_gsm")
        if requested_gsm:
            gsm_gap = abs(int(paper.gsm) - int(requested_gsm))
            score += max(0.0, 15.0 - float(gsm_gap) / 10.0)
        
        requested_type = (payload.get("paper_type") or "").strip().lower()
        if requested_type:
            paper_type = (paper.get_paper_type_display() or paper.paper_type or "").strip().lower()
            if requested_type in paper_type:
                score += 10.0
    elif material:
        requested_material_type = (payload.get("material_type") or "").strip().lower()
        if requested_material_type and requested_material_type in (material.material_type or "").strip().lower():
            score += 25.0
    
    # finishing exact/similar: +20
    if payload.get("finishing_ids") or payload.get("finishing_slugs") or payload.get("finishings"):
        # If we reached here and can_calculate is True, it means finishings matched
        if can_calculate:
            score += 20.0
        else:
            score += 10.0 # Partial match
            
    # pricing rates available: +15
    if getattr(shop, "pricing_ready", False):
        score += 15.0
        
    # can handle quantity/dimensions: +10
    if can_calculate:
        score += 10.0

    # Distance bonus (extra)
    distance_km = _distance_km_for_payload(shop, payload)
    if distance_km is not None:
        score += max(0.0, 10.0 - min(distance_km, 50.0) / 5.0)

    return round(min(score, 100.0), 2)


def _infer_product_pricing_mode(payload: dict[str, Any]) -> str:
    if payload.get("material_id"):
        return "LARGE_FORMAT"
    return "SHEET"


def _build_shop_row(
    shop: Shop,
    can_calculate: bool,
    *,
    total: str | None = None,
    currency: str | None = None,
    reason: str = "",
    missing_fields: list[str] | None = None,
    preview: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
    similarity_score: float = 0.0,
    distance_km: float | None = None,
    exact_or_estimated: bool = False,
    quote_basis: str = "manual_quote",
    product_match: Product | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    matched_specs = []
    if payload:
        if payload.get("job_type"):
            matched_specs.append(payload["job_type"].capitalize())
        elif payload.get("product_family"):
            matched_specs.append(payload["product_family"].replace("_", " ").capitalize())
        
        if payload.get("width_mm") and payload.get("height_mm"):
            matched_specs.append(f"{payload['width_mm']} × {payload['height_mm']} mm")
    
    if selection:
        if selection.get("paper_label"):
            matched_specs.append("Similar paper available")
        if selection.get("material_label"):
            matched_specs.append("Material available")

    needs_confirmation = []
    closest_alternatives = []
    if not can_calculate:
        if "paper" in (missing_fields or []):
            needs_confirmation.append("Exact paper stock needs confirmation")
            if selection and selection.get("paper_label"):
                closest_alternatives.append({
                    "type": "paper",
                    "requested": payload.get("paper_type") or (f"{payload.get('paper_gsm')}gsm" if payload.get("paper_gsm") else "requested stock"),
                    "available": selection["paper_label"],
                    "confidence": "similar",
                    "message": "Similar paper found"
                })
        if "material" in (missing_fields or []):
            needs_confirmation.append("Material needs confirmation")
            if selection and selection.get("material_label"):
                closest_alternatives.append({
                    "type": "material",
                    "requested": payload.get("material_type") or "requested material",
                    "available": selection["material_label"],
                    "confidence": "similar",
                    "message": "Similar material found"
                })
        if "finishing" in (missing_fields or []):
            needs_confirmation.append("Finishing price not listed yet")
        if not exact_or_estimated:
            needs_confirmation.append("Turnaround needs confirmation")

    row = {
        "id": shop.id,
        "shop_id": shop.id,
        "name": shop.name,
        "shop_name": shop.name,
        "slug": shop.slug,
        "shop_slug": shop.slug,
        "can_calculate": can_calculate,
        "can_price_now": can_calculate,
        "can_send_quote_request": True,
        "currency": currency or getattr(shop, "currency", "KES") or "KES",
        "reason": reason,
        "summary": reason,
        "missing_fields": _unique_strings(missing_fields or []),
        "total": total,
        "preview": preview,
        "selection": selection or {},
        "similarity_score": similarity_score,
        "confidence_score": similarity_score,
        "distance_km": distance_km,
        "exact_or_estimated": exact_or_estimated,
        "quote_basis": quote_basis,
        "match_type": "exact" if similarity_score >= 80 else ("close" if similarity_score >= 50 else "needs_confirmation"),
        "match_score": similarity_score,
        "price_confidence": "high" if exact_or_estimated and similarity_score >= 80 else ("medium" if exact_or_estimated else "low"),
        "product_match": {
            "id": product_match.id,
            "name": product_match.name,
            "slug": product_match.slug,
        } if product_match else None,
        "matched_specs": matched_specs,
        "needs_confirmation": needs_confirmation,
        "closest_alternatives": closest_alternatives,
        "production_preview": _extract_production_preview(preview) if preview else None,
        "pricing_breakdown": _extract_pricing_breakdown(preview) if preview else None,
        "missing_specs": _unique_strings(missing_fields or []),
        "alternative_suggestions": closest_alternatives,
        "estimated_price": _as_float(total),
        "price_range": None,
        "distance_label": f"{distance_km:.1f} km away" if distance_km is not None else None,
    }
    return row


def _extract_production_preview(preview: dict[str, Any]) -> dict[str, Any]:
    breakdown = preview.get("breakdown") or {}
    imposition = breakdown.get("imposition") or {}
    paper = breakdown.get("paper") or {}
    roll_usage = breakdown.get("roll_usage") or {}
    dimensions = breakdown.get("dimensions") or {}
    pricing = breakdown.get("pricing") or {}

    if preview.get("quote_type") == "large_format":
        return {
            "pieces_per_sheet": None,
            "sheets_required": None,
            "parent_sheet": None,
            "imposition_label": None,
            "size_label": preview.get("size_label"),
            "quantity": preview.get("quantity"),
            "cutting_required": False,
            "selected_finishings": [f.get("name") for f in (breakdown.get("finishings") or []) if f.get("name")],
            "suggested_finishings": [],
            "warnings": preview.get("explanations") or [],
            "roll_width_m": (
                round(float(roll_usage.get("roll_width_mm")) / 1000, 3)
                if roll_usage.get("roll_width_mm") not in (None, "")
                else None
            ),
            "roll_width_mm": roll_usage.get("roll_width_mm"),
            "items_per_row": roll_usage.get("items_per_row") or preview.get("items_per_row"),
            "rows": roll_usage.get("rows") or preview.get("rows"),
            "used_length_m": preview.get("used_length_m"),
            "orientation": roll_usage.get("orientation") or preview.get("orientation"),
            "input_size_m": {
                "width": round(float(dimensions.get("width_mm")) / 1000, 3),
                "height": round(float(dimensions.get("height_mm")) / 1000, 3),
            } if dimensions.get("width_mm") and dimensions.get("height_mm") else None,
            "charged_area_m2": preview.get("charged_area_m2") or pricing.get("charged_area_m2"),
            "printed_area_m2": preview.get("printed_area_m2"),
            "waste_area_m2": preview.get("waste_area_m2"),
            "overlap_area_m2": preview.get("overlap_area_m2"),
            "tiling": preview.get("tiling") or breakdown.get("tiling"),
        }

    return {
        "pieces_per_sheet": imposition.get("copies_per_sheet") or preview.get("copies_per_sheet"),
        "sheets_required": imposition.get("good_sheets") or preview.get("good_sheets"),
        "parent_sheet": imposition.get("sheet_size") or paper.get("sheet_size") or preview.get("parent_sheet_name"),
        "imposition_label": imposition.get("explanation"),
        "size_label": paper.get("label"),
        "quantity": preview.get("quantity"),
        "cutting_required": True if imposition.get("good_sheets") else False,
        "selected_finishings": [f.get("name") for f in (breakdown.get("finishings") or [])],
        "suggested_finishings": [],
        "warnings": preview.get("explanations") or [],
    }


def _extract_pricing_breakdown(preview: dict[str, Any]) -> dict[str, Any]:
    totals = preview.get("totals") or {}
    breakdown = preview.get("breakdown") or {}
    per_sheet = breakdown.get("per_sheet_pricing") or {}
    pricing = breakdown.get("pricing") or preview.get("pricing") or {}
    material = breakdown.get("material") or {}

    return {
        "currency": preview.get("currency") or "KES",
        "paper_price": _as_float(per_sheet.get("paper_price")),
        "print_price_front": _as_float(per_sheet.get("print_price_front")),
        "print_price_back": _as_float(per_sheet.get("print_price_back")),
        "total_per_sheet": _as_float(per_sheet.get("total_per_sheet")),
        "estimated_total": _as_float(totals.get("grand_total")),
        "price_range": None,
        "formula": per_sheet.get("formula"),
        "method": pricing.get("method"),
        "rate": _as_float(pricing.get("rate") if pricing.get("rate") is not None else material.get("rate_per_unit")),
        "charged_area_m2": _as_float(pricing.get("charged_area_m2")),
        "charged_length_m": _as_float(pricing.get("charged_length_m")),
        "minimum_charge": _as_float(pricing.get("minimum_charge")),
        "minimum_charge_applied": pricing.get("minimum_charge_applied"),
        "lines": [
            {"label": item.get("label"), "amount": item.get("amount"), "formula": item.get("formula")}
            for item in (preview.get("calculation_result", {}).get("line_items") or [])
        ]
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return None


def _paper_label(paper: Paper) -> str:
    return paper.marketplace_label


def _build_marketplace_summary(successful_rows: list[dict[str, Any]], failed_rows: list[dict[str, Any]]) -> str:
    if successful_rows:
        return f"Found {len(successful_rows)} shop matches with backend pricing previews."
    if failed_rows:
        return "No exact backend preview yet. Complete the missing requirements to unlock price ranges."
    return "No public shops are ready for this request yet."


def _unique_strings(values) -> list[str]:
    result: list[str] = []
    for value in values:
        if not value:
            continue
        text = str(value)
        if text not in result:
            result.append(text)
    return result


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


# ---------------------------------------------------------------------------
# Booklet marketplace matching
# ---------------------------------------------------------------------------

def get_booklet_marketplace_matches(payload: dict[str, Any]) -> dict[str, Any]:
    """Match shops that can price the given booklet spec, job-first (no upfront shop selection)."""
    candidate_shops = list(_filter_booklet_candidate_shops(payload))
    rows = [_preview_booklet_for_shop(shop, payload) for shop in candidate_shops]
    rows = [row for row in rows if row is not None]

    successful_rows = [row for row in rows if row["can_calculate"]]
    failed_rows = [row for row in rows if not row["can_calculate"]]
    return _build_marketplace_response(successful_rows=successful_rows, failed_rows=failed_rows)


def _filter_booklet_candidate_shops(payload: dict[str, Any]):
    """Filter to public shops that can actually offer booklet work."""
    queryset = (
        Shop.objects.filter(public_match_ready=True, is_active=True, is_public=True, supports_custom_requests=True)
        .filter(
            papers__is_active=True,
            papers__selling_price__gt=0,
            machines__is_active=True,
            machines__printing_rates__is_active=True,
            products__is_active=True,
            products__is_public=True,
            products__pricing_mode=PricingMode.SHEET,
            products__product_kind=ProductKind.BOOKLET,
        )
        .distinct()
    )
    return _apply_radius_filter(queryset, payload).distinct()


def _preview_booklet_for_shop(shop: Shop, payload: dict[str, Any]) -> dict[str, Any] | None:
    from services.pricing.booklet import calculate_booklet_pricing  # local import to avoid cycles

    cover_payload = {
        "paper_type": payload.get("cover_paper_type", ""),
        "paper_gsm": payload.get("cover_paper_gsm"),
        "sheet_size": payload.get("sheet_size", ""),
    }
    insert_payload = {
        "paper_type": payload.get("insert_paper_type", ""),
        "paper_gsm": payload.get("insert_paper_gsm"),
        "sheet_size": payload.get("sheet_size", ""),
    }
    cover_paper = _pick_paper(shop, cover_payload)
    insert_paper = _pick_paper(shop, insert_payload)

    missing_fields: list[str] = []
    width_mm = int(payload.get("width_mm") or 0)
    height_mm = int(payload.get("height_mm") or 0)
    if not width_mm:
        missing_fields.append("width_mm")
    if not height_mm:
        missing_fields.append("height_mm")
    if not cover_paper:
        missing_fields.append("cover_paper")
    if not insert_paper:
        missing_fields.append("insert_paper")

    if missing_fields:
        return _build_shop_row(
            shop,
            can_calculate=False,
            reason="Shop does not have papers set up for booklet printing." if not cover_paper or not insert_paper else "Booklet size is required.",
            missing_fields=missing_fields,
            similarity_score=_booklet_similarity_score(shop, payload, False),
            quote_basis="insufficient_data",
            payload=payload,
        )

    binding_type = payload.get("binding_type", "saddle_stitch")
    cover_lamination_mode = payload.get("cover_lamination_mode", "none")
    binding_rate = _resolve_binding_rate_for_shop(shop, binding_type)
    lamination_rate = _resolve_lamination_rate_for_shop(shop, cover_lamination_mode)

    # If lamination requested but unavailable, downgrade gracefully
    effective_lamination_mode = cover_lamination_mode
    if cover_lamination_mode != "none" and not lamination_rate:
        effective_lamination_mode = "none"

    try:
        result = calculate_booklet_pricing(
            shop=shop,
            quantity=payload.get("quantity") or 1,
            width_mm=width_mm,
            height_mm=height_mm,
            total_pages=payload.get("total_pages") or 12,
            binding_type=binding_type,
            cover_paper=cover_paper,
            insert_paper=insert_paper,
            cover_sides=payload.get("cover_sides", "DUPLEX"),
            insert_sides=payload.get("insert_sides", "DUPLEX"),
            cover_color_mode=payload.get("cover_color_mode", "COLOR"),
            insert_color_mode=payload.get("insert_color_mode", "COLOR"),
            cover_lamination_mode=effective_lamination_mode,
            cover_lamination_finishing_rate=lamination_rate,
            finishing_selections=[],
            binding_finishing_rate=binding_rate,
            turnaround_hours=payload.get("turnaround_hours"),
        )
    except Exception:
        return _build_shop_row(
            shop,
            can_calculate=False,
            reason="Could not price this booklet spec for this shop.",
            similarity_score=_booklet_similarity_score(shop, payload, False, cover_paper=cover_paper, insert_paper=insert_paper),
            quote_basis="manual_quote",
            payload=payload,
        )

    if result.get("can_calculate") is False:
        return _build_shop_row(
            shop,
            can_calculate=False,
            reason=result.get("reason") or "Booklet pricing failed for this shop.",
            similarity_score=_booklet_similarity_score(shop, payload, False, cover_paper=cover_paper, insert_paper=insert_paper),
            quote_basis="manual_quote",
            payload=payload,
        )

    grand_total = result.get("totals", {}).get("grand_total")
    selection: dict[str, Any] = {
        "cover_paper_id": cover_paper.id,
        "cover_paper_label": _paper_label(cover_paper),
        "insert_paper_id": insert_paper.id,
        "insert_paper_label": _paper_label(insert_paper),
    }
    if binding_rate:
        selection["binding_rate_id"] = binding_rate.id
        selection["binding_rate_label"] = binding_rate.name

    row = _build_shop_row(
        shop,
        can_calculate=True,
        total=str(grand_total) if grand_total is not None else None,
        currency=result.get("currency", getattr(shop, "currency", "KES") or "KES"),
        reason="Booklet preview from this shop.",
        preview=result,
        selection=selection,
        similarity_score=_booklet_similarity_score(shop, payload, True, cover_paper=cover_paper, insert_paper=insert_paper),
        exact_or_estimated=True,
        quote_basis="rate_card",
        payload=payload,
    )
    return _attach_turnaround(shop, row, payload)


def _resolve_binding_rate_for_shop(shop: Shop, binding_type: str) -> FinishingRate | None:
    tokens = {
        "saddle_stitch": ("saddle", "stitch"),
        "perfect_bind": ("perfect", "bind"),
        "wire_o": ("wire", "wire-o", "wireo"),
    }.get(binding_type, ())
    if not tokens:
        return None
    for finishing in FinishingRate.objects.filter(shop=shop, is_active=True).order_by("id"):
        haystacks = (
            (finishing.name or "").strip().lower(),
            (finishing.slug or "").strip().lower(),
        )
        if any(any(token in h for token in tokens) for h in haystacks):
            return finishing
    return None


def _resolve_lamination_rate_for_shop(shop: Shop, lamination_mode: str) -> FinishingRate | None:
    if lamination_mode == "none":
        return None
    for finishing in FinishingRate.objects.filter(shop=shop, is_active=True).order_by("id"):
        if finishing.is_lamination_rule():
            return finishing
    return None


def _booklet_similarity_score(
    shop: Shop,
    payload: dict[str, Any],
    can_calculate: bool,
    *,
    cover_paper: Paper | None = None,
    insert_paper: Paper | None = None,
) -> float:
    score = 0.0
    
    # same product/category: +30
    score += _product_availability_score(shop, "booklet") * 2
    if can_calculate:
        score += 30.0

    # paper exact/similar: +25
    if cover_paper:
        requested_cover_gsm = payload.get("cover_paper_gsm")
        if requested_cover_gsm:
            gsm_gap = abs(int(cover_paper.gsm) - int(requested_cover_gsm))
            score += max(0.0, 10.0 - float(gsm_gap) / 10.0)
        requested_cover_type = (payload.get("cover_paper_type") or "").strip().lower()
        if requested_cover_type:
            paper_type = (cover_paper.get_paper_type_display() or cover_paper.paper_type or "").strip().lower()
            if requested_cover_type in paper_type:
                score += 5.0
                
    if insert_paper:
        requested_insert_gsm = payload.get("insert_paper_gsm")
        if requested_insert_gsm:
            gsm_gap = abs(int(insert_paper.gsm) - int(requested_insert_gsm))
            score += max(0.0, 5.0 - float(gsm_gap) / 10.0)
        requested_insert_type = (payload.get("insert_paper_type") or "").strip().lower()
        if requested_insert_type:
            paper_type = (insert_paper.get_paper_type_display() or insert_paper.paper_type or "").strip().lower()
            if requested_insert_type in paper_type:
                score += 5.0

    # finishing exact/similar: +20
    if can_calculate:
        score += 20.0
    
    # pricing rates available: +15
    if getattr(shop, "pricing_ready", False):
        score += 15.0
        
    # can handle quantity/dimensions: +10
    if can_calculate:
        score += 10.0

    # Distance bonus
    distance_km = _distance_km_for_payload(shop, payload)
    if distance_km is not None:
        score += max(0.0, 10.0 - min(distance_km, 50.0) / 5.0)

    return round(min(score, 100.0), 2)
