from __future__ import annotations

from collections import OrderedDict
from typing import Any

from django.db.models import Q
from django.utils.text import slugify

from inventory.choices import PaperCategory
from inventory.models import Paper
from pricing.choices import Sides
from pricing.models import FinishingRate
from shops.models import Shop


COLOR_MODES = [
    {"value": "COLOR", "label": "Full color"},
    {"value": "BW", "label": "Black and white"},
]

PRINT_SIDES = [
    {"value": Sides.SIMPLEX, "label": "Single sided"},
    {"value": Sides.DUPLEX, "label": "Double sided"},
]

SHAPES = [
    {"value": "rectangle", "label": "Rectangle"},
    {"value": "square", "label": "Square"},
    {"value": "circle", "label": "Circle"},
    {"value": "oval", "label": "Oval"},
    {"value": "custom", "label": "Custom"},
]

CUT_TYPES = [
    {"value": "kiss_cut", "label": "Kiss cut"},
    {"value": "die_cut", "label": "Die cut"},
    {"value": "straight_cut", "label": "Straight cut"},
]

SIZE_LIBRARY = {
    "business_card": [
        {"value": "90x55mm", "label": "90 x 55 mm", "width_mm": 90, "height_mm": 55},
        {"value": "85x55mm", "label": "85 x 55 mm", "width_mm": 85, "height_mm": 55},
    ],
    "flyer": [
        {"value": "A6", "label": "A6", "width_mm": 105, "height_mm": 148},
        {"value": "A5", "label": "A5", "width_mm": 148, "height_mm": 210},
        {"value": "A4", "label": "A4", "width_mm": 210, "height_mm": 297},
        {"value": "A3", "label": "A3", "width_mm": 297, "height_mm": 420},
    ],
    "label_sticker": [
        {"value": "A6", "label": "A6", "width_mm": 105, "height_mm": 148},
        {"value": "A5", "label": "A5", "width_mm": 148, "height_mm": 210},
        {"value": "A4", "label": "A4", "width_mm": 210, "height_mm": 297},
        {"value": "100x50mm", "label": "100 x 50 mm", "width_mm": 100, "height_mm": 50},
    ],
    "letterhead": [
        {"value": "A4", "label": "A4", "width_mm": 210, "height_mm": 297},
        {"value": "A5", "label": "A5", "width_mm": 148, "height_mm": 210},
    ],
    "booklet": [
        {"value": "A5", "label": "A5", "width_mm": 148, "height_mm": 210},
        {"value": "A4", "label": "A4", "width_mm": 210, "height_mm": 297},
    ],
}


PRODUCT_DEFINITIONS = OrderedDict(
    [
        (
            "business_card",
            {
                "label": "Business Cards",
                "required_fields": ["quantity", "finished_size", "paper_stock", "print_sides", "color_mode"],
                "optional_fields": ["requested_paper_category", "requested_gsm", "lamination", "corner_rounding"],
                "defaults": {
                    "quantity": 100,
                    "finished_size": "90x55mm",
                    "print_sides": "DUPLEX",
                    "color_mode": "COLOR",
                    "lamination": "none",
                    "corner_rounding": False,
                },
                "allowed_paper_categories": ["artcard", "matt", "gloss", "ivory", "special", "other"],
                "allowed_finishings": ["lamination", "corner_rounding"],
                "allowed_print_sides": ["SIMPLEX", "DUPLEX"],
            },
        ),
        (
            "flyer",
            {
                "label": "Flyers",
                "required_fields": ["quantity", "finished_size", "paper_stock", "print_sides", "color_mode"],
                "optional_fields": ["requested_paper_category", "requested_gsm", "folding", "lamination"],
                "defaults": {
                    "quantity": 100,
                    "finished_size": "A5",
                    "print_sides": "SIMPLEX",
                    "color_mode": "COLOR",
                    "folding": "none",
                    "lamination": "none",
                },
                "allowed_paper_categories": ["matt", "gloss", "bond", "ivory", "special", "other"],
                "allowed_finishings": ["folding", "lamination"],
                "allowed_print_sides": ["SIMPLEX", "DUPLEX"],
            },
        ),
        (
            "label_sticker",
            {
                "label": "Label Stickers / Tictac",
                "required_fields": ["quantity", "finished_size", "paper_stock", "shape", "cut_type", "color_mode"],
                "optional_fields": ["requested_paper_category", "requested_gsm", "lamination"],
                "defaults": {
                    "quantity": 100,
                    "finished_size": "100x50mm",
                    "shape": "rectangle",
                    "cut_type": "kiss_cut",
                    "color_mode": "COLOR",
                    "lamination": "none",
                },
                "allowed_paper_categories": ["tictac", "special", "other"],
                "allowed_finishings": ["lamination", "cutting"],
                "allowed_print_sides": ["SIMPLEX"],
            },
        ),
        (
            "letterhead",
            {
                "label": "Letterheads / Conqueror",
                "required_fields": ["quantity", "finished_size", "paper_stock", "print_sides", "color_mode"],
                "optional_fields": ["requested_paper_category", "requested_gsm"],
                "defaults": {
                    "quantity": 100,
                    "finished_size": "A4",
                    "print_sides": "SIMPLEX",
                    "color_mode": "COLOR",
                },
                "allowed_paper_categories": ["bond", "conqueror", "ivory", "special", "other"],
                "allowed_finishings": [],
                "allowed_print_sides": ["SIMPLEX", "DUPLEX"],
            },
        ),
        (
            "booklet",
            {
                "label": "Booklets",
                "required_fields": ["quantity", "finished_size", "total_pages", "cover_stock", "insert_stock"],
                "optional_fields": ["cover_lamination", "binding_type", "cutting"],
                "defaults": {
                    "quantity": 100,
                    "finished_size": "A5",
                    "total_pages": 12,
                    "binding_type": "saddle_stitch",
                    "cover_lamination": "none",
                    "cutting": True,
                },
                "allowed_cover_categories": ["artcard", "matt", "gloss", "cover_board", "ivory", "conqueror", "special", "other"],
                "allowed_insert_categories": ["bond", "matt", "gloss", "ivory", "conqueror", "special", "other"],
                "allowed_finishings": ["cover_lamination", "stitching", "cutting", "binding"],
                "allowed_print_sides": ["DUPLEX"],
            },
        ),
    ]
)


def _paper_queryset():
    public_shop_ids = Shop.objects.filter(is_active=True, is_public=True).values_list("id", flat=True)
    return Paper.objects.filter(shop_id__in=public_shop_ids, is_active=True, selling_price__gt=0)


def _paper_option_key(paper: Paper) -> str:
    return slugify(f"{paper.category}-{paper.marketplace_label}-{paper.gsm}") or f"paper-{paper.id}"


def _paper_option(paper: Paper) -> dict[str, Any]:
    return {
        "key": _paper_option_key(paper),
        "label": paper.marketplace_label,
        "display_name": paper.marketplace_label,
        "category": paper.category,
        "category_label": paper.category_label,
        "gsm": paper.gsm,
        "paper_type": paper.paper_type,
        "is_cover_stock": paper.supports_usage("cover"),
        "is_insert_stock": paper.supports_usage("insert"),
        "is_sticker_stock": paper.supports_usage("sticker"),
        "is_specialty": bool(paper.is_specialty),
    }


def _aggregate_paper_stocks() -> list[dict[str, Any]]:
    options: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for paper in _paper_queryset().order_by("category", "gsm", "display_name", "id"):
        option = _paper_option(paper)
        options.setdefault(option["key"], option)
    return list(options.values())


def _aggregate_finishings() -> list[dict[str, Any]]:
    rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
    shop_ids = Shop.objects.filter(is_active=True, is_public=True).values_list("id", flat=True)
    queryset = (
        FinishingRate.objects.filter(shop_id__in=shop_ids, is_active=True)
        .select_related("category")
        .order_by("slug", "name", "id")
    )
    for finishing in queryset:
        key = (finishing.slug or slugify(finishing.name or "")).strip()
        if not key:
            continue
        rows.setdefault(
            key,
            {
                "key": key,
                "label": finishing.name,
                "slug": key,
                "category": finishing.category.slug if finishing.category else "",
                "help_text": finishing.help_text or "",
            },
        )
    return list(rows.values())


def _paper_categories() -> list[dict[str, Any]]:
    return [{"value": value, "label": label} for value, label in PaperCategory.choices]


def _field_definitions(product_key: str, definition: dict[str, Any], paper_stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    category_field_key = "allowed_paper_categories"
    stock_usage_filter = lambda item: True
    stock_field = "paper_stock"
    requested_category_label = "Requested paper category"
    if product_key == "booklet":
        return [
            {"key": "quantity", "label": "Quantity", "type": "number", "required": True, "help_text": "How many finished booklets do you need?"},
            {"key": "finished_size", "label": "Finished size", "type": "select", "required": True, "options": SIZE_LIBRARY["booklet"]},
            {"key": "total_pages", "label": "Total pages", "type": "number", "required": True, "help_text": "Include every page. Backend will normalize to a production-safe multiple of 4."},
            {
                "key": "cover_stock",
                "label": "Cover stock",
                "type": "select",
                "required": True,
                "options": [item for item in paper_stocks if item["category"] in definition["allowed_cover_categories"] and item["is_cover_stock"]],
            },
            {
                "key": "insert_stock",
                "label": "Insert stock",
                "type": "select",
                "required": True,
                "options": [item for item in paper_stocks if item["category"] in definition["allowed_insert_categories"] and item["is_insert_stock"]],
            },
            {"key": "cover_lamination", "label": "Cover lamination", "type": "select", "required": False, "options": [{"value": "none", "label": "No lamination"}, {"value": "front", "label": "Front only"}, {"value": "both", "label": "Both sides"}], "help_text": "Lamination is applied to cover sheets only."},
            {"key": "binding_type", "label": "Binding", "type": "select", "required": False, "options": [{"value": "saddle_stitch", "label": "Saddle stitch"}, {"value": "perfect_bind", "label": "Perfect bind"}, {"value": "wire_o", "label": "Wire-O"}]},
            {"key": "cutting", "label": "Cutting", "type": "boolean", "required": False, "help_text": "Backend applies cutting only when the shop has a matching finishing path."},
        ]

    if product_key == "label_sticker":
        stock_usage_filter = lambda item: item["category"] in definition["allowed_paper_categories"] and item["is_sticker_stock"]
    else:
        stock_usage_filter = lambda item: item["category"] in definition["allowed_paper_categories"]

    fields = [
        {"key": "quantity", "label": "Quantity", "type": "number", "required": True},
        {"key": "finished_size", "label": "Finished size", "type": "select", "required": True, "options": SIZE_LIBRARY[product_key]},
        {"key": stock_field, "label": "Paper stock", "type": "select", "required": True, "options": [item for item in paper_stocks if stock_usage_filter(item)]},
        {"key": "requested_paper_category", "label": requested_category_label, "type": "select", "required": False, "options": [item for item in _paper_categories() if item["value"] in definition["allowed_paper_categories"]], "help_text": "Optional override when the client requests a category that may need a closest-stock match."},
        {"key": "requested_gsm", "label": "Requested GSM", "type": "number", "required": False, "help_text": "Optional exact grammage request. Backend will match the nearest available stock if needed."},
        {"key": "color_mode", "label": "Color mode", "type": "select", "required": True, "options": COLOR_MODES},
    ]
    if definition["allowed_print_sides"]:
        fields.append({"key": "print_sides", "label": "Print sides", "type": "select", "required": "print_sides" in definition["required_fields"], "options": [item for item in PRINT_SIDES if item["value"] in definition["allowed_print_sides"]]})
    if product_key == "business_card":
        fields.extend(
            [
                {"key": "lamination", "label": "Lamination", "type": "select", "required": False, "options": [{"value": "none", "label": "No lamination"}, {"value": "gloss-lamination", "label": "Gloss lamination"}, {"value": "matt-lamination", "label": "Matt lamination"}]},
                {"key": "corner_rounding", "label": "Corner rounding", "type": "boolean", "required": False},
            ]
        )
    elif product_key == "flyer":
        fields.extend(
            [
                {"key": "folding", "label": "Folding", "type": "select", "required": False, "options": [{"value": "none", "label": "No folding"}, {"value": "half-fold", "label": "Half fold"}, {"value": "tri-fold", "label": "Tri-fold"}]},
                {"key": "lamination", "label": "Lamination", "type": "select", "required": False, "options": [{"value": "none", "label": "No lamination"}, {"value": "gloss-lamination", "label": "Gloss lamination"}, {"value": "matt-lamination", "label": "Matt lamination"}]},
            ]
        )
    elif product_key == "label_sticker":
        fields.extend(
            [
                {"key": "shape", "label": "Shape", "type": "select", "required": True, "options": SHAPES},
                {"key": "cut_type", "label": "Cut type", "type": "select", "required": True, "options": CUT_TYPES},
                {"key": "lamination", "label": "Lamination", "type": "select", "required": False, "options": [{"value": "none", "label": "No lamination"}, {"value": "gloss-lamination", "label": "Gloss lamination"}, {"value": "matt-lamination", "label": "Matt lamination"}]},
            ]
        )
    return fields


def get_calculator_config() -> dict[str, Any]:
    paper_stocks = _aggregate_paper_stocks()
    finishings = _aggregate_finishings()
    products = []
    for product_key, definition in PRODUCT_DEFINITIONS.items():
        fields = _field_definitions(product_key, definition, paper_stocks)
        products.append(
            {
                "key": product_key,
                "label": definition["label"],
                "required_fields": definition["required_fields"],
                "optional_fields": definition["optional_fields"],
                "defaults": definition["defaults"],
                "allowed_paper_categories": definition.get("allowed_paper_categories", []),
                "allowed_cover_categories": definition.get("allowed_cover_categories", []),
                "allowed_insert_categories": definition.get("allowed_insert_categories", []),
                "allowed_finishings": definition["allowed_finishings"],
                "allowed_print_sides": definition["allowed_print_sides"],
                "sizes": SIZE_LIBRARY[product_key],
                "fields": fields,
            }
        )
    return {
        "products": products,
        "paper_categories": _paper_categories(),
        "paper_stocks": paper_stocks,
        "finishings": finishings,
        "sizes": SIZE_LIBRARY,
        "print_sides": PRINT_SIDES,
        "color_modes": COLOR_MODES,
        "preview_endpoint": "/api/calculator/public-preview/",
    }


def get_product_definition(product_type: str) -> dict[str, Any] | None:
    return PRODUCT_DEFINITIONS.get(product_type)


def resolve_finished_size(product_type: str, finished_size: str | None) -> dict[str, Any] | None:
    value = (finished_size or "").strip()
    if not value:
        return None
    for option in SIZE_LIBRARY.get(product_type, []):
        if option["value"] == value:
            return option
    return None


def resolve_stock_option(stock_key: str | None, usage: str = "") -> dict[str, Any] | None:
    key = (stock_key or "").strip()
    if not key:
        return None
    for option in _aggregate_paper_stocks():
        if option["key"] == key:
            if usage == "cover" and not option["is_cover_stock"]:
                return None
            if usage == "insert" and not option["is_insert_stock"]:
                return None
            if usage == "sticker" and not option["is_sticker_stock"]:
                return None
            return option
    return None
