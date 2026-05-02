import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from services.pricing.calculator_config import get_calculator_config
import json

try:
    config = get_calculator_config()
    lf = next(p for p in config['products'] if p['key'] == 'large_format')
    material_field = next(f for f in lf['fields'] if f['key'] == 'material_type')
    print(f"Material options: {json.dumps(material_field['options'], indent=2)}")
except Exception as e:
    print(f"ERROR: {e}")
