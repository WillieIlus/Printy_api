import os
import django
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from services.pricing.calculator_preview import build_public_calculator_preview
import json

payload = {
    "product_type": "large_format",
    "quantity": 1,
    "product_subtype": "banner",
    "material_type": "PVC Banner",
    "width_mm": 1000,
    "height_mm": 1000,
}

try:
    result = build_public_calculator_preview(payload)
    # Filter out bulky match data for readability
    summary = {
        "can_calculate": result.get("can_calculate"),
        "total": result.get("total"),
        "currency": result.get("currency"),
        "matches_count": result.get("matches_count"),
        "message": result.get("message"),
        "missing_fields": result.get("missing_fields"),
        "pricing_breakdown": result.get("pricing_breakdown")
    }
    print(json.dumps(summary, indent=2))
except Exception as e:
    import traceback
    print(f"ERROR: {e}")
    traceback.print_exc()
