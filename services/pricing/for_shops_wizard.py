from __future__ import annotations

from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from django.db.models import Avg
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError

from inventory.choices import MachineType, PaperCategory, PaperType, SheetSize
from inventory.models import Machine, Paper
from pricing.choices import (
    ChargeUnit,
    ColorMode,
    FinishingBillingBasis,
    FinishingSideMode,
    Sides,
)
from pricing.models import FinishingCategory, FinishingRate, Material, PrintingRate
from services.pricing.imposition import build_imposition_breakdown
from services.pricing.booklet_builder import build_booklet_preview
from services.pricing.quote_builder import build_quote_preview
from services.public_matching import recompute_shop_match_readiness
from setup.services import get_setup_status_for_shop


BUSINESS_CARD_WIDTH_MM = 90
BUSINESS_CARD_HEIGHT_MM = 55
FLYER_WIDTH_MM = 148
FLYER_HEIGHT_MM = 210
BOOKLET_WIDTH_MM = 148
BOOKLET_HEIGHT_MM = 210
SRA3_WIDTH_MM = 320
SRA3_HEIGHT_MM = 450
PUBLIC_SUGGESTED_PRICE_MULTIPLIER = Decimal("1.55")


STEP_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": "business_cards",
        "title": "Business cards",
        "description": "Set the baseline rates most shops reuse everywhere else.",
        "progress_label": "Step 1 of 3",
        "product_type": "business_card",
        "preview": {
            "quantity": 500,
            "width_mm": BUSINESS_CARD_WIDTH_MM,
            "height_mm": BUSINESS_CARD_HEIGHT_MM,
            "color_mode": ColorMode.COLOR,
            "sides": Sides.DUPLEX,
        },
        "default_spec": {
            "product_type": "business_card",
            "quantity": 500,
            "finished_size": "90x55mm",
            "press_sheet": SheetSize.SRA3,
            "paper_name": "350gsm Art Card",
            "paper_gsm": 350,
            "color_mode": ColorMode.COLOR,
            "print_sides": Sides.DUPLEX,
            "finishing": ["matte-lamination", "cutting"],
        },
        "fields": [
            {
                "key": "business_cards_paper_price",
                "kind": "paper",
                "field": "selling_price",
                "label": "350gsm Art Card",
                "description": "Price you charge or use per SRA3 sheet.",
                "help_text": "If you know your buying price separately, keep that in your paper row. This wizard updates the sheet sell-side price used by Printy.",
                "unit": "per SRA3 sheet",
                "placeholder": "e.g. 1200",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "gsm": 350,
                    "category": PaperCategory.ARTCARD,
                },
            },
            {
                "key": "business_cards_print_single_price",
                "kind": "printing",
                "field": "single_price",
                "label": "Full-color print rate",
                "description": "Charge for printing one SRA3 side in full color.",
                "help_text": "Use the per-side rate your shop actually wants to bill or model.",
                "unit": "per SRA3 side",
                "placeholder": "e.g. 150",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "color_mode": ColorMode.COLOR,
                },
            },
            {
                "key": "business_cards_print_double_price",
                "kind": "printing",
                "field": "double_price",
                "label": "Duplex override",
                "description": "Optional all-in duplex price for both printed sides on one sheet.",
                "help_text": "Leave blank if duplex should simply be calculated from the single-side price plus any surcharge rule.",
                "unit": "per SRA3 sheet",
                "placeholder": "Optional",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "color_mode": ColorMode.COLOR,
                },
            },
            {
                "key": "business_cards_duplex_surcharge",
                "kind": "printing",
                "field": "duplex_surcharge",
                "label": "Duplex surcharge",
                "description": "Optional surcharge added once per duplex SRA3 sheet.",
                "help_text": "Use this only if your shop bills a separate duplex surcharge on top of the per-side print price.",
                "unit": "per SRA3 sheet",
                "placeholder": "Optional",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "color_mode": ColorMode.COLOR,
                },
            },
            {
                "key": "business_cards_lamination_price",
                "kind": "finishing",
                "field": "price",
                "label": "Matte lamination",
                "description": "One-side matte lamination price for an SRA3 sheet.",
                "help_text": "Save the one-side rate first. The both-sides field can override it after that.",
                "unit": "per SRA3 sheet",
                "placeholder": "e.g. 50",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "matte-lamination"},
            },
            {
                "key": "business_cards_lamination_double_side_price",
                "kind": "finishing",
                "field": "double_side_price",
                "label": "Matte lamination both sides",
                "description": "Optional both-sides lamination price for one SRA3 sheet.",
                "help_text": "If blank, pricing falls back to twice the one-side lamination rate.",
                "unit": "per SRA3 sheet",
                "placeholder": "Optional",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "matte-lamination"},
            },
            {
                "key": "business_cards_cutting_price",
                "kind": "finishing",
                "field": "price",
                "label": "Cut to size",
                "description": "Total cutting charge for the full business-card job.",
                "help_text": "This is saved as the shop's cutting finishing rule.",
                "unit": "per job",
                "placeholder": "e.g. 300",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "cutting"},
            },
        ],
    },
    {
        "key": "flyers",
        "title": "Flyers",
        "description": "Reuse your full-color sheet logic on a lighter stock.",
        "progress_label": "Step 2 of 3",
        "product_type": "flyer",
        "preview": {
            "quantity": 100,
            "width_mm": FLYER_WIDTH_MM,
            "height_mm": FLYER_HEIGHT_MM,
            "color_mode": ColorMode.COLOR,
            "sides": Sides.DUPLEX,
        },
        "default_spec": {
            "product_type": "flyer",
            "quantity": 100,
            "finished_size": "A5",
            "press_sheet": SheetSize.SRA3,
            "paper_name": "150gsm Art Paper",
            "paper_gsm": 150,
            "color_mode": ColorMode.COLOR,
            "print_sides": Sides.DUPLEX,
            "finishing": ["cutting"],
        },
        "fields": [
            {
                "key": "flyers_paper_price",
                "kind": "paper",
                "field": "selling_price",
                "label": "150gsm Art Paper",
                "description": "Price you charge or use per SRA3 flyer sheet.",
                "help_text": "This row must already exist in the shop paper catalog before the wizard can update it.",
                "unit": "per SRA3 sheet",
                "placeholder": "e.g. 900",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "gsm": 150,
                },
            },
            {
                "key": "flyers_print_single_price",
                "kind": "printing",
                "field": "single_price",
                "label": "Full-color print rate",
                "description": "Charge for printing one SRA3 side in full color.",
                "help_text": "This attaches to the active sheet-fed machine rate for the shop.",
                "unit": "per SRA3 side",
                "placeholder": "e.g. 150",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "color_mode": ColorMode.COLOR,
                },
            },
            {
                "key": "flyers_print_double_price",
                "kind": "printing",
                "field": "double_price",
                "label": "Duplex override",
                "description": "Optional full duplex price for one SRA3 sheet.",
                "help_text": "Use this if duplex does not equal two single sides in your shop.",
                "unit": "per SRA3 sheet",
                "placeholder": "Optional",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "color_mode": ColorMode.COLOR,
                },
            },
            {
                "key": "flyers_cutting_price",
                "kind": "finishing",
                "field": "price",
                "label": "Cutting only",
                "description": "Cutting charge for the full flyer job.",
                "help_text": "Saved to the shop cutting finishing rule.",
                "unit": "per job",
                "placeholder": "e.g. 250",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "cutting"},
            },
        ],
    },
    {
        "key": "booklets",
        "title": "Booklets",
        "description": "Cover stock, insert stock, printing, lamination, and binding in one pass.",
        "progress_label": "Step 3 of 3",
        "product_type": "booklet",
        "preview": {
            "quantity": 100,
            "width_mm": BOOKLET_WIDTH_MM,
            "height_mm": BOOKLET_HEIGHT_MM,
            "total_pages": 12,
            "binding_type": "saddle_stitch",
            "cover_sides": Sides.SIMPLEX,
            "insert_sides": Sides.DUPLEX,
            "cover_color_mode": ColorMode.COLOR,
            "insert_color_mode": ColorMode.COLOR,
            "cover_lamination_mode": "front",
        },
        "default_spec": {
            "product_type": "booklet",
            "quantity": 100,
            "finished_size": "A5",
            "total_pages": 12,
            "press_sheet": SheetSize.SRA3,
            "cover_name": "250gsm Art Card",
            "cover_gsm": 250,
            "insert_name": "130gsm Art Paper",
            "insert_gsm": 130,
            "color_mode": ColorMode.COLOR,
            "insert_print_sides": Sides.DUPLEX,
            "cover_lamination": "matte-lamination",
            "binding": "saddle-stitch",
            "cutting": "cutting",
        },
        "fields": [
            {
                "key": "booklets_cover_paper_price",
                "kind": "paper",
                "field": "selling_price",
                "label": "250gsm Art Card cover",
                "description": "Price per SRA3 cover sheet.",
                "help_text": "Use the same art-card pricing logic you want applied to booklet covers.",
                "unit": "per SRA3 sheet",
                "placeholder": "e.g. 1100",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "gsm": 250,
                    "category": PaperCategory.ARTCARD,
                },
            },
            {
                "key": "booklets_insert_paper_price",
                "kind": "paper",
                "field": "selling_price",
                "label": "130gsm Art Paper inserts",
                "description": "Price per SRA3 insert sheet.",
                "help_text": "This row must exist in the paper catalog before the wizard can update it.",
                "unit": "per SRA3 sheet",
                "placeholder": "e.g. 700",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "gsm": 130,
                },
            },
            {
                "key": "booklets_print_single_price",
                "kind": "printing",
                "field": "single_price",
                "label": "Full-color print rate",
                "description": "Charge for printing one SRA3 side in full color.",
                "help_text": "This is reused for both cover and insert printing in the preview.",
                "unit": "per SRA3 side",
                "placeholder": "e.g. 150",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "color_mode": ColorMode.COLOR,
                },
            },
            {
                "key": "booklets_print_double_price",
                "kind": "printing",
                "field": "double_price",
                "label": "Duplex override",
                "description": "Optional duplex price per SRA3 sheet.",
                "help_text": "If blank, duplex pricing falls back to the single-side rate logic.",
                "unit": "per SRA3 sheet",
                "placeholder": "Optional",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {
                    "sheet_size": SheetSize.SRA3,
                    "color_mode": ColorMode.COLOR,
                },
            },
            {
                "key": "booklets_lamination_price",
                "kind": "finishing",
                "field": "price",
                "label": "Matte lamination",
                "description": "One-side lamination rate used on booklet covers.",
                "help_text": "Set this before adding the both-sides override if your cover lamination differs.",
                "unit": "per SRA3 sheet",
                "placeholder": "e.g. 50",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "matte-lamination"},
            },
            {
                "key": "booklets_lamination_double_side_price",
                "kind": "finishing",
                "field": "double_side_price",
                "label": "Matte lamination both sides",
                "description": "Optional both-sides cover lamination price.",
                "help_text": "If blank, the pricing engine falls back to doubling the one-side lamination rate.",
                "unit": "per SRA3 sheet",
                "placeholder": "Optional",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "matte-lamination"},
            },
            {
                "key": "booklets_binding_price",
                "kind": "finishing",
                "field": "price",
                "label": "Saddle stitch",
                "description": "Binding charge saved to the shop saddle-stitch rule.",
                "help_text": "The preview uses this as the booklet binding cost.",
                "unit": "per booklet",
                "placeholder": "e.g. 40",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "saddle-stitch"},
            },
            {
                "key": "booklets_cutting_price",
                "kind": "finishing",
                "field": "price",
                "label": "Cut to size",
                "description": "Cutting charge for the completed booklet job.",
                "help_text": "Saved to the cutting finishing rule and reused across wizard steps.",
                "unit": "per job",
                "placeholder": "e.g. 300",
                "validation": {"min": 0, "max": 50000, "step": 0.01},
                "lookup": {"slug": "cutting"},
            },
        ],
    },
]


def _to_decimal(value: Any, *, allow_null: bool = False) -> Decimal | None:
    if value in (None, ""):
        if allow_null:
            return None
        raise ValidationError("A numeric value is required.")
    try:
        return Decimal(str(value))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("Enter a valid numeric amount.") from exc


def _median_mean(values: list[Decimal]) -> dict[str, str | None]:
    if not values:
        return {"median": None, "mean": None}
    ordered = sorted(values)
    mid = Decimal(str(median(ordered))).quantize(Decimal("0.01"))
    mean_value = (sum(ordered) / Decimal(len(ordered))).quantize(Decimal("0.01"))
    return {
        "median": str(mid),
        "mean": str(mean_value),
    }


def _decimal_to_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))


def _value_to_string(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_to_string(value)
    return value


def _market_stats(values: list[Decimal], *, minimum_sample_size_for_display: int = 3) -> dict[str, Any]:
    summary = _median_mean(values)
    return {
        "sample_size": len(values),
        "median": summary["median"] if len(values) >= minimum_sample_size_for_display else None,
        "mean": summary["mean"] if len(values) >= minimum_sample_size_for_display else None,
        "minimum_sample_size_for_display": minimum_sample_size_for_display,
        "has_enough_data": len(values) >= minimum_sample_size_for_display,
    }


def _market_range(values: list[Decimal]) -> dict[str, Any]:
    if not values:
        return {
            "min": None,
            "max": None,
            "median": None,
            "mean": None,
            "sample_size": 0,
        }
    ordered = sorted(values)
    summary = _median_mean(values)
    return {
        "min": _decimal_to_string(ordered[0]),
        "max": _decimal_to_string(ordered[-1]),
        "median": summary["median"],
        "mean": summary["mean"],
        "sample_size": len(values),
    }


def _preferred_public_default(market: dict[str, Any]) -> str | None:
    return market.get("median") or market.get("mean")


def _step_completion(step: dict[str, Any], field_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    required_fields = [field for field in step["fields"] if field.get("required", True) is not False]
    required_keys = {field["key"] for field in required_fields}
    saved_required = [
        payload for payload in field_payloads
        if payload["key"] in required_keys and payload.get("saved_value") not in (None, "")
    ]
    missing_required = [
        payload["key"] for payload in field_payloads
        if payload["key"] in required_keys and payload.get("saved_value") in (None, "")
    ]
    is_complete = len(saved_required) == len(required_keys)
    return {
        "is_complete": is_complete,
        "saved_required_fields": len(saved_required),
        "required_field_count": len(required_keys),
        "missing_required_fields": missing_required,
    }


def _sheet_machine(shop) -> Machine | None:
    return (
        Machine.objects.filter(shop=shop, is_active=True)
        .exclude(machine_type=MachineType.LARGE_FORMAT)
        .order_by(
            "machine_type",
            "id",
        )
        .first()
    )


def _find_step(step_key: str) -> dict[str, Any]:
    for step in STEP_DEFINITIONS:
        if step["key"] == step_key:
            return step
    raise ValidationError({"step_key": [f"Unknown wizard step '{step_key}'."]})


def _paper_queryset(shop, lookup: dict[str, Any]):
    queryset = Paper.objects.filter(shop=shop, is_active=True)
    if lookup.get("sheet_size"):
        queryset = queryset.filter(sheet_size=lookup["sheet_size"])
    if lookup.get("gsm"):
        queryset = queryset.filter(gsm=lookup["gsm"])
    if lookup.get("category"):
        queryset = queryset.filter(category=lookup["category"])
    return queryset.order_by("id")


def _resolve_paper(shop, lookup: dict[str, Any]) -> Paper | None:
    return _paper_queryset(shop, lookup).first()


def _resolve_printing_rate(shop, lookup: dict[str, Any]) -> tuple[Machine | None, PrintingRate | None]:
    machine = _sheet_machine(shop)
    if not machine:
        return None, None
    rate = PrintingRate.objects.filter(
        machine=machine,
        sheet_size=lookup["sheet_size"],
        color_mode=lookup["color_mode"],
        is_active=True,
    ).order_by("id").first()
    return machine, rate


def _resolve_finishing(shop, lookup: dict[str, Any]) -> FinishingRate | None:
    slug = lookup.get("slug", "")
    if not slug:
        return None
    return (
        FinishingRate.objects.filter(shop=shop, is_active=True)
        .filter(slug=slug)
        .select_related("category")
        .order_by("id")
        .first()
    )


def _paper_market(lookup: dict[str, Any]) -> tuple[list[Decimal], dict[str, Any]]:
    queryset = Paper.objects.filter(is_active=True)
    if lookup.get("sheet_size"):
        queryset = queryset.filter(sheet_size=lookup["sheet_size"])
    if lookup.get("gsm"):
        queryset = queryset.filter(gsm=lookup["gsm"])
    if lookup.get("category"):
        queryset = queryset.filter(category=lookup["category"])
    values = [Decimal(str(value)) for value in queryset.values_list("selling_price", flat=True)]
    return values, _market_stats(values)


def _printing_market(lookup: dict[str, Any], field_name: str) -> tuple[list[Decimal], dict[str, Any]]:
    queryset = PrintingRate.objects.filter(is_active=True)
    queryset = queryset.filter(
        sheet_size=lookup["sheet_size"],
        color_mode=lookup["color_mode"],
    )
    values = [Decimal(str(value)) for value in queryset.values_list(field_name, flat=True) if value is not None]
    return values, _market_stats(values)


def _finishing_market(lookup: dict[str, Any], field_name: str) -> tuple[list[Decimal], dict[str, Any]]:
    queryset = FinishingRate.objects.filter(is_active=True)
    if lookup.get("slug"):
        queryset = queryset.filter(slug=lookup["slug"])
    values = [Decimal(str(value)) for value in queryset.values_list(field_name, flat=True) if value is not None]
    return values, _market_stats(values)


def _build_field_payload(shop, field_definition: dict[str, Any]) -> dict[str, Any]:
    lookup = dict(field_definition["lookup"])
    saved_value = None
    save_error = ""

    if field_definition["kind"] == "paper":
        record = _resolve_paper(shop, lookup)
        saved_value = getattr(record, field_definition["field"], None) if record else None
        market_values, market_stats = _paper_market(lookup)
        if record is None:
            save_error = "Create this paper in your materials first so the wizard can update its shop price."
    elif field_definition["kind"] == "printing":
        machine, record = _resolve_printing_rate(shop, lookup)
        saved_value = getattr(record, field_definition["field"], None) if record else None
        lookup["machine_id"] = machine.id if machine else None
        lookup["machine_name"] = machine.name if machine else None
        market_values, market_stats = _printing_market(lookup, field_definition["field"])
        if machine is None:
            save_error = "Add a digital or offset machine first so Printy can save printing rates."
        elif record is None and field_definition["field"] != "single_price":
            save_error = "Save the base print rate first so the duplex override can attach to an existing printing rate."
    else:
        record = _resolve_finishing(shop, lookup)
        saved_value = getattr(record, field_definition["field"], None) if record else None
        market_values, market_stats = _finishing_market(lookup, field_definition["field"])
        if record is None and field_definition["field"] != "price":
            save_error = "Save the base finishing rate first so the extra finishing field can attach to it."

    return {
        "key": field_definition["key"],
        "label": field_definition["label"],
        "description": field_definition.get("description", ""),
        "help_text": field_definition.get("help_text", ""),
        "unit": field_definition["unit"],
        "placeholder": field_definition.get("placeholder", ""),
        "required": field_definition.get("required", True),
        "validation": field_definition.get("validation") or {"min": 0, "max": 50000, "step": 0.01},
        "value": _value_to_string(saved_value),
        "current_value": _value_to_string(saved_value),
        "saved_value": _value_to_string(saved_value),
        "has_saved_value": saved_value is not None,
        "model": {
            "paper": "Paper",
            "printing": "PrintingRate",
            "finishing": "FinishingRate",
        }[field_definition["kind"]],
        "field": field_definition["field"],
        "lookup": lookup,
        "market": {
            "median": _median_mean(market_values)["median"] if market_values else None,
            "mean": _median_mean(market_values)["mean"] if market_values else None,
        },
        "market_stats": market_stats,
        "save_error": save_error,
        "input_type": "currency",
        "currency": getattr(shop, "currency", "KES") or "KES",
        "backend": {
            "model": {
                "paper": "Paper",
                "printing": "PrintingRate",
                "finishing": "FinishingRate",
            }[field_definition["kind"]],
            "field": field_definition["field"],
            "lookup": lookup,
        },
        "market_value_count": len(market_values),
    }


def _field_value_map(values: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for item in values:
        key = item.get("key")
        if isinstance(key, str):
            output[key] = item.get("value")
    return output


def _money_to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def build_rate_wizard_config(shop) -> dict[str, Any]:
    machine = _sheet_machine(shop)
    setup_status = get_setup_status_for_shop(shop)
    step_payloads: list[dict[str, Any]] = []
    for index, step in enumerate(STEP_DEFINITIONS, start=1):
        fields = [_build_field_payload(shop, field) for field in step["fields"]]
        completion = _step_completion(step, fields)
        step_payloads.append(
            {
                "key": step["key"],
                "id": step["key"],
                "title": step["title"],
                "description": step.get("description", ""),
                "order": index,
                "progress_label": step["progress_label"],
                "product_type": step["product_type"],
                "default_spec": step["default_spec"],
                "preview_spec": step["preview"],
                "fields": fields,
                "completion": completion,
                "is_complete": completion["is_complete"],
            }
        )

    wizard_complete = all(step["is_complete"] for step in step_payloads) if step_payloads else False
    return {
        "shop": {
            "id": shop.id,
            "slug": shop.slug,
            "name": shop.name,
            "currency": getattr(shop, "currency", "KES") or "KES",
        },
        "shop_id": shop.id,
        "shop_has_completed_onboarding": wizard_complete,
        "setup_status": setup_status,
        "requirements": {
            "sheet_machine_required": machine is not None,
            "sheet_machine": {
                "id": machine.id if machine else None,
                "name": machine.name if machine else None,
                "machine_type": machine.machine_type if machine else None,
            },
            "note": ""
            if machine is not None
            else "No active digital/offset machine is configured yet, so printing-rate fields cannot be saved until one exists.",
        },
        "save_endpoint": "/api/for-shops/rate-wizard/save-step/",
        "preview_endpoint": "/api/for-shops/rate-wizard/preview/",
        "complete_endpoint": "/api/for-shops/rate-wizard/complete/",
        "field_catalog": {
            "paper": ["selling_price"],
            "printing": ["single_price", "double_price", "duplex_surcharge"],
            "finishing": ["price", "double_side_price"],
        },
        "wizard": {
            "version": "1.0",
            "title": "Set up your rates",
            "description": "Configure pricing for your top products using the shop models you already own.",
            "steps": step_payloads,
            "is_complete": wizard_complete,
            "completed_step_count": sum(1 for step in step_payloads if step["is_complete"]),
            "step_count": len(step_payloads),
        },
        "available_materials": [
            {
                "id": material.id,
                "material_type": material.material_type,
                "unit": material.unit,
                "selling_price": str(material.selling_price),
                "print_price_per_sqm": str(material.print_price_per_sqm),
            }
            for material in Material.objects.filter(shop=shop, is_active=True).order_by("material_type", "id")
        ],
        "available_papers": [
            {
                "id": paper.id,
                "display_name": paper.marketplace_label,
                "sheet_size": paper.sheet_size,
                "gsm": paper.gsm,
                "category": paper.category,
                "selling_price": str(paper.selling_price),
                "buying_price": str(paper.buying_price),
                "is_cover_stock": paper.is_cover_stock,
                "is_insert_stock": paper.is_insert_stock,
            }
            for paper in Paper.objects.filter(shop=shop, is_active=True).order_by("sheet_size", "gsm", "id")
        ],
        "available_finishing_rates": [
            {
                "id": rate.id,
                "name": rate.name,
                "slug": rate.slug,
                "price": str(rate.price),
                "double_side_price": str(rate.double_side_price) if rate.double_side_price is not None else None,
                "category": getattr(rate.category, "slug", ""),
            }
            for rate in FinishingRate.objects.filter(shop=shop, is_active=True).select_related("category").order_by("name", "id")
        ],
        "steps": step_payloads,
    }


def _ensure_finishing_category(name: str, slug: str) -> FinishingCategory:
    category = FinishingCategory.objects.filter(slug=slug).first()
    if category:
        return category
    return FinishingCategory.objects.create(name=name, slug=slug)


def _upsert_finishing(shop, lookup: dict[str, Any], value_map: dict[str, Any], field_name: str) -> FinishingRate:
    existing = _resolve_finishing(shop, lookup)
    if existing:
        setattr(existing, field_name, _to_decimal(value_map[field_name], allow_null=field_name == "double_side_price"))
        existing.save()
        return existing

    if field_name != "price":
        raise ValidationError({"values": [f"Save the base rate for '{lookup['slug']}' before saving {field_name}."]})

    price = _to_decimal(value_map["price"])
    if lookup["slug"] == "matte-lamination":
        category = _ensure_finishing_category("Lamination", "lamination")
        rate = FinishingRate.objects.create(
            shop=shop,
            category=category,
            name="Matte Lamination",
            slug="matte-lamination",
            charge_unit=ChargeUnit.PER_SHEET,
            billing_basis=FinishingBillingBasis.PER_SHEET,
            side_mode=FinishingSideMode.PER_SELECTED_SIDE,
            display_unit_label="per sheet",
            help_text="Charged per sheet. Choose one side or both sides.",
            price=price,
            is_active=True,
        )
    elif lookup["slug"] == "saddle-stitch":
        category = _ensure_finishing_category("Binding", "binding")
        rate = FinishingRate.objects.create(
            shop=shop,
            category=category,
            name="Saddle Stitch",
            slug="saddle-stitch",
            charge_unit=ChargeUnit.FLAT,
            billing_basis=FinishingBillingBasis.FLAT_PER_JOB,
            side_mode=FinishingSideMode.IGNORE_SIDES,
            display_unit_label="per booklet",
            help_text="Charged once per booklet job.",
            price=price,
            is_active=True,
        )
    else:
        category = _ensure_finishing_category("Cutting", "cutting")
        rate = FinishingRate.objects.create(
            shop=shop,
            category=category,
            name="Cutting",
            slug="cutting",
            charge_unit=ChargeUnit.FLAT,
            billing_basis=FinishingBillingBasis.FLAT_PER_JOB,
            side_mode=FinishingSideMode.IGNORE_SIDES,
            display_unit_label="per job",
            help_text="Charged once per cutting job.",
            price=price,
            is_active=True,
        )
    return rate


def _upsert_printing(shop, lookup: dict[str, Any], value_map: dict[str, Any], field_name: str) -> PrintingRate:
    machine = _sheet_machine(shop)
    if machine is None:
        raise ValidationError({"values": ["Add a machine before saving printing rates."]})

    existing = PrintingRate.objects.filter(
        machine=machine,
        sheet_size=lookup["sheet_size"],
        color_mode=lookup["color_mode"],
    ).first()
    if existing:
        setattr(existing, field_name, _to_decimal(value_map[field_name], allow_null=field_name == "double_price"))
        if field_name == "duplex_surcharge":
            existing.duplex_surcharge_enabled = bool(_to_decimal(value_map[field_name]) and _to_decimal(value_map[field_name]) > 0)
        existing.is_active = True
        existing.save()
        return existing

    if field_name not in {"single_price", "duplex_surcharge"}:
        raise ValidationError({"values": ["Save the base print rate first before saving duplex override pricing."]})

    return PrintingRate.objects.create(
        machine=machine,
        sheet_size=lookup["sheet_size"],
        color_mode=lookup["color_mode"],
        single_price=_to_decimal(value_map.get("single_price") or "0"),
        double_price=_to_decimal(value_map.get("double_price"), allow_null=True),
        duplex_surcharge=_to_decimal(value_map.get("duplex_surcharge") or "0"),
        duplex_surcharge_enabled=bool(_to_decimal(value_map.get("duplex_surcharge") or "0") > 0),
        is_active=True,
        is_default=not PrintingRate.objects.filter(machine=machine, is_default=True).exists(),
    )


def _upsert_paper(shop, lookup: dict[str, Any], value_map: dict[str, Any]) -> Paper:
    paper = _resolve_paper(shop, lookup)
    if paper is None:
        label = f"{lookup.get('gsm', '')}gsm {lookup.get('category', 'paper')}".strip()
        raise ValidationError(
            {
                "values": [
                    f"{label} does not exist in this shop yet. Create the paper row first so the wizard can update its selling price without inventing a buying price."
                ]
            }
        )
    paper.selling_price = _to_decimal(value_map["selling_price"])
    paper.is_active = True
    paper.save()
    return paper


def save_step_values(shop, step_key: str, values: list[dict[str, Any]]) -> dict[str, Any]:
    step = _find_step(step_key)
    provided = _field_value_map(values)
    saved: list[dict[str, Any]] = []

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for field in step["fields"]:
        if field["key"] not in provided:
            continue
        group_key = (field["kind"], slugify(str(field["lookup"])))
        grouped.setdefault(
            group_key,
            {
                "kind": field["kind"],
                "lookup": field["lookup"],
                "values": {},
            },
        )
        grouped[group_key]["values"][field["field"]] = provided[field["key"]]

    for payload in grouped.values():
        kind = payload["kind"]
        if kind == "paper":
            record = _upsert_paper(shop, payload["lookup"], payload["values"])
        elif kind == "printing":
            record = None
            for field_name in ("single_price", "double_price", "duplex_surcharge"):
                if field_name in payload["values"]:
                    record = _upsert_printing(shop, payload["lookup"], payload["values"], field_name)
        else:
            record = None
            for field_name in ("price", "double_side_price"):
                if field_name in payload["values"]:
                    record = _upsert_finishing(shop, payload["lookup"], payload["values"], field_name)

        if record is not None:
            saved.append(
                {
                    "model": record.__class__.__name__,
                    "id": record.id,
                    "lookup": payload["lookup"],
                }
            )

    recompute_shop_match_readiness(shop)
    config = build_rate_wizard_config(shop)
    saved_step = next((item for item in config["steps"] if item["key"] == step_key), None)
    next_step = next(
        (item for item in config["steps"] if item["key"] != step_key and not item.get("is_complete")),
        None,
    )
    return {
        "step_key": step_key,
        "saved": saved,
        "step": saved_step,
        "next_step": {
            "key": next_step["key"],
            "title": next_step["title"],
        } if next_step else None,
        "wizard_complete": bool(config.get("shop_has_completed_onboarding")),
        "setup_status": config["setup_status"],
    }


def _format_preview_validation(can_calculate: bool, reason: str = "", warnings: list[str] | None = None) -> dict[str, Any]:
    issues = [item for item in (warnings or []) if item]
    errors = [reason] if reason else []
    return {
        "is_valid": can_calculate,
        "errors": errors,
        "warnings": issues,
    }


def _build_sheet_retail_scenarios(step: dict[str, Any], pricing: dict[str, Any]) -> list[dict[str, Any]]:
    totals = pricing.get("totals", {})
    unit_price = Decimal(str(totals.get("unit_price") or "0"))
    quantity = int(step["preview"]["quantity"])
    sheet_count = int(pricing.get("good_sheets") or 0)
    base_total = Decimal(str(totals.get("grand_total") or "0"))
    scenarios: list[dict[str, Any]] = []
    for multiplier in (Decimal("1.20"), Decimal("1.45"), Decimal("1.70")):
        retail_per_piece = (unit_price * multiplier).quantize(Decimal("0.01"))
        revenue = (retail_per_piece * Decimal(quantity)).quantize(Decimal("0.01"))
        margin = (revenue - base_total).quantize(Decimal("0.01"))
        margin_percent = Decimal("0.00")
        if revenue > 0:
            margin_percent = ((margin / revenue) * Decimal("100")).quantize(Decimal("0.01"))
        scenarios.append(
            {
                "label": f"{int(multiplier * 100)}% of current estimate",
                "retail_per_unit": _decimal_to_string(retail_per_piece),
                "order_quantity": quantity,
                "total_sheets_needed": sheet_count,
                "expected_revenue": _decimal_to_string(revenue),
                "expected_gross_margin": _decimal_to_string(margin),
                "expected_margin_percent": _decimal_to_string(margin_percent),
            }
        )
    return scenarios


def _build_sheet_line_items(pricing: dict[str, Any], *, lamination_slug: str | None = None) -> list[dict[str, Any]]:
    totals = pricing.get("totals", {})
    breakdown = pricing.get("breakdown", {})
    good_sheets = int(pricing.get("good_sheets") or 0)
    line_items: list[dict[str, Any]] = []

    paper = breakdown.get("paper", {}) or {}
    paper_rate = _money_to_decimal(paper.get("paper_price_per_sheet") or paper.get("cost_per_sheet"))
    paper_total = _money_to_decimal(totals.get("paper_cost"))
    if paper_total > 0:
        line_items.append(
            {
                "key": "paper",
                "label": paper.get("label") or "Paper",
                "rate": _decimal_to_string(paper_rate),
                "quantity": good_sheets,
                "total": _decimal_to_string(paper_total),
            }
        )

    printing = breakdown.get("printing", {}) or {}
    printing_rate = _money_to_decimal(printing.get("rate_per_sheet") or printing.get("total_per_sheet"))
    print_total = _money_to_decimal(totals.get("print_cost"))
    if print_total > 0:
        line_items.append(
            {
                "key": "printing",
                "label": "Full color duplex" if printing.get("sides") == Sides.DUPLEX else "Printing",
                "rate": _decimal_to_string(printing_rate),
                "quantity": good_sheets,
                "total": _decimal_to_string(print_total),
            }
        )

    finishing_lines = breakdown.get("finishings") or []
    for finishing in finishing_lines:
        total = _money_to_decimal(finishing.get("total"))
        if total <= 0:
            continue
        units = _money_to_decimal(finishing.get("units") or "0")
        rate = _money_to_decimal(finishing.get("rate"))
        slug = str(finishing.get("slug") or "")
        line_items.append(
            {
                "key": slug or "finishing",
                "label": finishing.get("name") or "Finishing",
                "rate": _decimal_to_string(rate),
                "quantity": int(units) if units == units.to_integral_value() else str(units),
                "total": _decimal_to_string(total),
                "is_lamination": bool(lamination_slug and slug == lamination_slug),
            }
        )
    return line_items


def _public_market_field(*, key: str, label: str, unit: str, values: list[Decimal]) -> dict[str, Any]:
    market = _market_range(values)
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "value": _preferred_public_default(market),
        "market": market,
    }


def build_public_rate_wizard_config() -> dict[str, Any]:
    paper_values = [
        Decimal(str(value))
        for value in Paper.objects.filter(
            is_active=True,
            sheet_size=SheetSize.SRA3,
            gsm=300,
            category=PaperCategory.ARTCARD,
        ).values_list("selling_price", flat=True)
    ]
    single_values = [
        Decimal(str(value))
        for value in PrintingRate.objects.filter(
            is_active=True,
            sheet_size=SheetSize.SRA3,
            color_mode=ColorMode.COLOR,
        ).values_list("single_price", flat=True)
        if value is not None
    ]
    double_values = [
        Decimal(str(value))
        for value in PrintingRate.objects.filter(
            is_active=True,
            sheet_size=SheetSize.SRA3,
            color_mode=ColorMode.COLOR,
        ).values_list("double_price", flat=True)
        if value is not None
    ]
    surcharge_values = [
        Decimal(str(value))
        for value in PrintingRate.objects.filter(
            is_active=True,
            sheet_size=SheetSize.SRA3,
            color_mode=ColorMode.COLOR,
        ).values_list("duplex_surcharge", flat=True)
        if value is not None
    ]
    lamination_values = [
        Decimal(str(value))
        for value in FinishingRate.objects.filter(
            is_active=True,
            slug="matte-lamination",
        ).values_list("price", flat=True)
        if value is not None
    ]
    cutting_values = [
        Decimal(str(value))
        for value in FinishingRate.objects.filter(
            is_active=True,
            slug="cutting",
        ).values_list("price", flat=True)
        if value is not None
    ]

    imposition = build_imposition_breakdown(
        quantity=500,
        finished_width_mm=BUSINESS_CARD_WIDTH_MM,
        finished_height_mm=BUSINESS_CARD_HEIGHT_MM,
        sheet_width_mm=SRA3_WIDTH_MM,
        sheet_height_mm=SRA3_HEIGHT_MM,
    )

    return {
        "preset": {
            "key": "business_cards",
            "title": "Business Cards",
            "quantity_default": 500,
            "quantity_options": [100, 500],
            "spec": {
                "paper_gsm": 300,
                "paper_name": "Art Card",
                "press_sheet": SheetSize.SRA3,
                "color_mode": "full_color",
                "print_sides": "duplex",
                "finishing": ["matte_lamination", "cutting"],
            },
        },
        "imposition": {
            "press_sheet": SheetSize.SRA3,
            "items_per_sheet": imposition.copies_per_sheet,
        },
        "fields": [
            _public_market_field(
                key="paper_300gsm_sra3",
                label="300gsm Art Card / SRA3",
                unit="per SRA3 sheet",
                values=paper_values,
            ),
            _public_market_field(
                key="print_single_side",
                label="Print single side",
                unit="per SRA3 side",
                values=single_values,
            ),
            _public_market_field(
                key="print_double_side",
                label="Print double side",
                unit="per SRA3 sheet",
                values=double_values,
            ),
            _public_market_field(
                key="surcharge",
                label="Duplex surcharge",
                unit="per SRA3 sheet",
                values=surcharge_values,
            ),
            _public_market_field(
                key="matte_lamination",
                label="Matte lamination",
                unit="per SRA3 sheet",
                values=lamination_values,
            ),
            _public_market_field(
                key="cutting",
                label="Cutting",
                unit="per job",
                values=cutting_values,
            ),
        ],
    }


def build_public_rate_wizard_preview(*, quantity: int, rates: dict[str, Any]) -> dict[str, Any]:
    imposition = build_imposition_breakdown(
        quantity=quantity,
        finished_width_mm=BUSINESS_CARD_WIDTH_MM,
        finished_height_mm=BUSINESS_CARD_HEIGHT_MM,
        sheet_width_mm=SRA3_WIDTH_MM,
        sheet_height_mm=SRA3_HEIGHT_MM,
    )
    good_sheets = imposition.good_sheets

    paper_rate = _money_to_decimal(rates.get("paper_300gsm_sra3"))
    print_single = _money_to_decimal(rates.get("print_single_side"))
    print_double = _money_to_decimal(rates.get("print_double_side"))
    surcharge = _money_to_decimal(rates.get("surcharge"))
    lamination = _money_to_decimal(rates.get("matte_lamination"))
    cutting = _money_to_decimal(rates.get("cutting"))

    line_items = [
        {
            "key": "paper",
            "label": "300gsm Art Card",
            "rate": _decimal_to_string(paper_rate),
            "quantity": good_sheets,
            "total": _decimal_to_string(paper_rate * Decimal(good_sheets)),
        },
        {
            "key": "printing",
            "label": "Full color duplex",
            "rate": _decimal_to_string(print_double if print_double > 0 else (print_single * Decimal("2"))),
            "quantity": good_sheets,
            "total": _decimal_to_string((print_double if print_double > 0 else (print_single * Decimal("2"))) * Decimal(good_sheets)),
        },
        {
            "key": "surcharge",
            "label": "Duplex surcharge",
            "rate": _decimal_to_string(surcharge),
            "quantity": good_sheets,
            "total": _decimal_to_string(surcharge * Decimal(good_sheets)),
        },
        {
            "key": "lamination",
            "label": "Matte lamination",
            "rate": _decimal_to_string(lamination),
            "quantity": good_sheets,
            "total": _decimal_to_string(lamination * Decimal(good_sheets)),
        },
        {
            "key": "cutting",
            "label": "Cutting",
            "rate": _decimal_to_string(cutting),
            "quantity": 1,
            "total": _decimal_to_string(cutting),
        },
    ]
    production_cost = sum((_money_to_decimal(item["total"]) for item in line_items), Decimal("0"))
    suggested_price = (production_cost * PUBLIC_SUGGESTED_PRICE_MULTIPLIER).quantize(Decimal("0.01"))

    return {
        "preset_key": "business_cards",
        "quantity": quantity,
        "imposition": {
            "press_sheet": SheetSize.SRA3,
            "items_per_sheet": imposition.copies_per_sheet,
            "sheets_needed": good_sheets,
        },
        "breakdown": line_items,
        "production_cost": _decimal_to_string(production_cost),
        "suggested_selling_price": _decimal_to_string(suggested_price),
    }


def _sheet_cost_breakdown(pricing: dict[str, Any]) -> dict[str, Any]:
    totals = pricing.get("totals", {})
    quantity = Decimal(str(pricing.get("quantity") or 0))
    safe_quantity = quantity if quantity > 0 else Decimal("1")
    paper_cost = Decimal(str(totals.get("paper_cost") or "0"))
    print_cost = Decimal(str(totals.get("print_cost") or "0"))
    finishing_cost = Decimal(str(totals.get("finishing_total") or "0"))
    total_cost = Decimal(str(totals.get("grand_total") or "0"))
    return {
        "paper_total": _decimal_to_string(paper_cost),
        "printing_total": _decimal_to_string(print_cost),
        "finishing_total": _decimal_to_string(finishing_cost),
        "paper_per_unit": _decimal_to_string((paper_cost / safe_quantity).quantize(Decimal("0.01"))),
        "printing_per_unit": _decimal_to_string((print_cost / safe_quantity).quantize(Decimal("0.01"))),
        "finishing_per_unit": _decimal_to_string((finishing_cost / safe_quantity).quantize(Decimal("0.01"))),
        "total_cost_per_unit": _decimal_to_string((total_cost / safe_quantity).quantize(Decimal("0.01"))),
    }


def _build_sheet_preview(shop, step: dict[str, Any], *, quantity: int | None = None) -> dict[str, Any]:
    preview = {
        **step["preview"],
        **({"quantity": quantity} if quantity is not None else {}),
    }
    if step["key"] == "business_cards":
        paper = _resolve_paper(shop, {"sheet_size": SheetSize.SRA3, "gsm": 350, "category": PaperCategory.ARTCARD})
    else:
        paper = _resolve_paper(shop, {"sheet_size": SheetSize.SRA3, "gsm": 150})
    machine = _sheet_machine(shop)
    lamination = _resolve_finishing(shop, {"slug": "matte-lamination"}) if step["key"] == "business_cards" else None
    cutting = _resolve_finishing(shop, {"slug": "cutting"})

    if paper is None:
        return {
            "step_key": step["key"],
            "can_calculate": False,
            "reason": "The required paper row is missing for this wizard step.",
            "missing_fields": ["paper"],
            "validation": _format_preview_validation(False, "The required paper row is missing for this wizard step."),
        }
    if machine is None:
        return {
            "step_key": step["key"],
            "can_calculate": False,
            "reason": "Add a machine before previewing this wizard step.",
            "missing_fields": ["machine"],
            "validation": _format_preview_validation(False, "Add a machine before previewing this wizard step."),
        }

    finishings = []
    if lamination is not None:
        finishings.append({"finishing_rate": lamination, "selected_side": "both"})
    if cutting is not None:
        finishings.append({"finishing_rate": cutting, "selected_side": "both"})

    pricing = build_quote_preview(
        shop=shop,
        quantity=preview["quantity"],
        paper=paper,
        machine=machine,
        color_mode=preview["color_mode"],
        sides=preview["sides"],
        finishing_selections=finishings,
        width_mm=preview["width_mm"],
        height_mm=preview["height_mm"],
    )
    total = pricing.get("totals", {}).get("grand_total")
    warnings = pricing.get("explanations", [])
    line_items = _build_sheet_line_items(pricing, lamination_slug="matte-lamination")
    return {
        "step_key": step["key"],
        "preset_key": step["key"],
        "quantity": preview["quantity"],
        "can_calculate": pricing.get("can_calculate", True),
        "imposition": pricing.get("breakdown", {}).get("imposition", {}),
        "imposition_summary": {
            "press_sheet": paper.sheet_size,
            "items_per_sheet": pricing.get("copies_per_sheet"),
            "sheets_needed": pricing.get("good_sheets"),
        },
        "sheet_count": pricing.get("good_sheets"),
        "production_cost": total,
        "suggested_price": total,
        "suggested_quote": total,
        "pricing_note": "Existing wizard pricing fields are sell-side rates, so production_cost and suggested_quote are the same until separate internal cost fields exist.",
        "cost_breakdown": _sheet_cost_breakdown(pricing),
        "retail_scenarios": _build_sheet_retail_scenarios(step, pricing),
        "line_items": line_items,
        "breakdown": pricing.get("breakdown", {}),
        "totals": pricing.get("totals", {}),
        "warnings": warnings,
        "validation": _format_preview_validation(pricing.get("can_calculate", True), "", warnings),
    }


def _build_booklet_preview(shop, step: dict[str, Any], *, quantity: int | None = None) -> dict[str, Any]:
    cover_paper = _resolve_paper(shop, {"sheet_size": SheetSize.SRA3, "gsm": 250, "category": PaperCategory.ARTCARD})
    insert_paper = _resolve_paper(shop, {"sheet_size": SheetSize.SRA3, "gsm": 130})
    lamination = _resolve_finishing(shop, {"slug": "matte-lamination"})
    binding = _resolve_finishing(shop, {"slug": "saddle-stitch"})
    cutting = _resolve_finishing(shop, {"slug": "cutting"})

    preview = {
        **step["preview"],
        **({"quantity": quantity} if quantity is not None else {}),
    }
    pricing = build_booklet_preview(
        shop=shop,
        quantity=preview["quantity"],
        width_mm=preview["width_mm"],
        height_mm=preview["height_mm"],
        total_pages=preview["total_pages"],
        binding_type=preview["binding_type"],
        cover_paper=cover_paper,
        insert_paper=insert_paper,
        cover_sides=preview["cover_sides"],
        insert_sides=preview["insert_sides"],
        cover_color_mode=preview["cover_color_mode"],
        insert_color_mode=preview["insert_color_mode"],
        cover_lamination_mode=preview["cover_lamination_mode"],
        cover_lamination_finishing_rate=lamination,
        binding_finishing_rate=binding,
        finishing_selections=(
            [{"finishing_rate": cutting, "selected_side": "both"}] if cutting is not None else []
        ),
    )
    total = pricing.get("totals", {}).get("grand_total")
    booklet_breakdown = pricing.get("breakdown", {}).get("booklet", {})
    totals = pricing.get("totals", {})
    warnings = pricing.get("warnings", []) + pricing.get("assumptions", [])
    quantity = Decimal(str(step["preview"]["quantity"] or 0))
    safe_quantity = quantity if quantity > 0 else Decimal("1")
    paper_total = Decimal(str(totals.get("paper_cost") or "0"))
    print_total = Decimal(str(totals.get("print_cost") or "0"))
    finishing_total = Decimal(str(totals.get("finishing_total") or "0"))
    grand_total = Decimal(str(totals.get("grand_total") or "0"))
    return {
        "step_key": step["key"],
        "can_calculate": pricing.get("can_calculate", False),
        "imposition": {
            "cover_sheets": pricing.get("cover_sheets"),
            "insert_sheets": pricing.get("insert_sheets"),
            "cover_up_per_sheet": pricing.get("cover_up_per_sheet"),
            "insert_up_per_sheet": pricing.get("insert_up_per_sheet"),
            "booklet": booklet_breakdown,
        },
        "sheet_count": (pricing.get("cover_sheets") or 0) + (pricing.get("insert_sheets") or 0),
        "production_cost": total,
        "suggested_quote": total,
        "pricing_note": "Existing wizard pricing fields are sell-side rates, so production_cost and suggested_quote are the same until separate internal cost fields exist.",
        "cost_breakdown": {
            "paper_total": _decimal_to_string(paper_total),
            "printing_total": _decimal_to_string(print_total),
            "finishing_total": _decimal_to_string(finishing_total),
            "paper_per_unit": _decimal_to_string((paper_total / safe_quantity).quantize(Decimal("0.01"))),
            "printing_per_unit": _decimal_to_string((print_total / safe_quantity).quantize(Decimal("0.01"))),
            "finishing_per_unit": _decimal_to_string((finishing_total / safe_quantity).quantize(Decimal("0.01"))),
            "total_cost_per_unit": _decimal_to_string((grand_total / safe_quantity).quantize(Decimal("0.01"))),
        },
        "retail_scenarios": _build_sheet_retail_scenarios(step, pricing),
        "breakdown": pricing.get("breakdown", {}),
        "totals": pricing.get("totals", {}),
        "warnings": warnings,
        "validation": _format_preview_validation(pricing.get("can_calculate", False), pricing.get("reason", ""), warnings),
    }


def build_step_preview(shop, step_key: str, *, quantity: int | None = None) -> dict[str, Any]:
    step = _find_step(step_key)
    if step["key"] == "booklets":
        return _build_booklet_preview(shop, step, quantity=quantity)
    return _build_sheet_preview(shop, step, quantity=quantity)


def complete_rate_wizard(shop) -> dict[str, Any]:
    recompute_shop_match_readiness(shop)
    status = get_setup_status_for_shop(shop)
    config = build_rate_wizard_config(shop)
    return {
        "completed": bool(config.get("shop_has_completed_onboarding")),
        "supports_explicit_completion": False,
        "message": "This backend computes readiness from the saved shop models. There is no separate onboarding-complete model to mark.",
        "setup_status": status,
        "wizard_summary": {
            "step_count": config["wizard"]["step_count"],
            "completed_step_count": config["wizard"]["completed_step_count"],
        },
    }
