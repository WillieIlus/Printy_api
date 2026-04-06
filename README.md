# Printy_API

Next generation print shop SaaS backend for Kenyan print shops.

## Stack
- Django 5.x
- Django REST Framework
- SimpleJWT
- PostgreSQL / SQLite (dev)

## Architecture Principles
- No redundant models
- Shop-scoped pricing
- No ambiguous price lookups
- FK-based quote calculations
- Separate Seller vs Buyer permissions

---

## Internationalization (i18n)

### Supported languages
- **English** (`en`)
- **Kiswahili** (`sw`)

### How language toggle works

1. **Authenticated users**: Set `preferred_language` via `PATCH /api/auth/me/`:
   ```json
   { "preferred_language": "sw" }
   ```
   The API activates this language for all subsequent requests. Default is `en`.

2. **Unauthenticated / fallback**: Send `Accept-Language` header:
   ```
   Accept-Language: sw
   ```

3. **Frontend guidance**:
   - Store user preference in profile; send on login.
   - For API calls, either set `Accept-Language` on each request or rely on `preferred_language` (persisted on user) after login.

### Adding translations (makemessages / compilemessages)

1. **Extract translatable strings** into a `.po` file for a locale:
   ```bash
   python manage.py makemessages -l sw
   ```
   This creates/updates `locale/sw/LC_MESSAGES/django.po`.

2. **Edit the `.po` file** and add translations for each `msgid`:
   ```po
   msgid "Hello"
   msgstr "Habari"
   ```

3. **Compile** the `.po` files into `.mo` binaries:
   ```bash
   python manage.py compilemessages
   ```

4. **Use in code**:
   ```python
   from django.utils.translation import gettext as _
   msg = _("Hello")
   ```

---

## Django admin

- **Shop admin**: Inlines for Paper, FinishingRate, Material, Machine, ServiceRate, Product. Edit all shop resources on one page.
- **Machine admin**: PrintingRate inline (one row per sheet_size + color_mode). Set Single/Double prices per sheet.
- **Finishing rates as inline**: Yes. `TabularInline` is formset-like—multiple related rows in a table. Shops have a small, fixed set of finishing options (lamination, cutting, etc.) best managed inline with the shop.

---

## Product price range & missing fields

### Price hint (`product_price_hint`)

Products expose `price_hint` in the catalog and shop product APIs:
```json
{
  "price_hint": {
    "can_calculate": true,
    "min_price": 150.0,
    "max_price": 450.0,
    "missing_fields": []
  }
}
```

When data is incomplete (includes actionable suggestions):
```json
{
  "price_hint": {
    "can_calculate": false,
    "min_price": null,
    "max_price": null,
    "missing_fields": ["paper", "machine", "printing_rate"],
    "suggestions": [
      {"code": "ADD_PAPER", "message": "Add paper with selling price under Shop → Papers.", "target": {"resource": "papers", "shop_id": 1}},
      {"code": "ADD_PRINTING_RATE", "message": "Set printing rate (single/double) for each machine + sheet size under Machine → Printing Rates.", "target": {"resource": "printing_rates", "shop_id": 1}}
    ],
    "reason": "Configure papers, machines, and rates under Shop setup."
  }
}
```

Display: "From KES X" when `min_price` is set; optionally "Up to KES Y" when `max_price` differs.

### Defaults used

- **quantity**: `min_quantity` (default 1)
- **size**: `default_finished_width_mm` / `default_finished_height_mm`; for LARGE_FORMAT, `min_width_mm` / `min_height_mm` override when set.

### What triggers "missing"

- **SHEET products**: Need `dimensions` (default_finished_width_mm, default_finished_height_mm), `paper` (with selling_price), `machine`, `printing_rate` (per sheet_size + color_mode).
- **LARGE_FORMAT products**: Need `material` (with selling_price), `dimensions` (default or min_width_mm/min_height_mm).

### Preview price response (standardized)

`POST /api/quote-drafts/{id}/preview-price/` always returns PricingDiagnostics:
- `can_calculate`: bool — true if all items can be priced
- `total`: number — 0 if none
- `lines`: list — breakdown lines (empty if none)
- `needs_review_items`: list[int] — item IDs that need more data
- `missing_fields`: list[str] — aggregated missing field names
- `reason`: str — optional human message (e.g. "2 item(s) need more details to calculate.")
- `suggestions`: list — actionable `{code, message, target}` (ADD_PAPER, ADD_PRINTING_RATE, ADD_DIMENSIONS, etc.)
- `item_diagnostics`: dict — `{item_id: {can_calculate, missing_fields, suggestions, reason}}` per item
- `items_missing_fields`: dict — `{item_id: [field_names]}` per item needing review

### Actionable suggestions (Kenyan-printshop friendly)

When pricing cannot be computed, the API returns clear guidance:
- **ADD_PAPER**: Add paper selling price for SRA3 200gsm under Shop → Papers
- **ADD_PRINTING_RATE**: Set Digital Press printing rate for SRA3 COLOR (single/double) under Machine → Printing Rates
- **ADD_DIMENSIONS**: Add artwork size so we can compute imposition (sheets needed)
- **SELECT_MACHINE**, **SELECT_SIDES**, **SELECT_COLOR_MODE**: Choose options for this item

---

## Frontend: repeatable forms (formsets)

For shop setup screens (papers bulk add, finishing list, printing rates grid), use a **dynamic array** pattern similar to Django formsets:

### Structure

1. **State**: `items = ref([{ ... }, { ... }])`
2. **Add row**: `items.value.push({ sheet_size: 'A4', ... })`
3. **Remove row**: `items.value.splice(i, 1)`
4. **Submit**: `POST` each item or `PATCH` in bulk if the API supports it.

### Example (Vue 3 + Nuxt)

```vue
<script setup>
const items = ref([{ sheet_size: 'A4', gsm: 80, selling_price: '2.00' }])
function addRow() {
  items.value.push({ sheet_size: 'A4', gsm: 80, selling_price: '' })
}
function removeRow(i) {
  items.value.splice(i, 1)
}
async function save() {
  for (const item of items.value) {
    await $api(`shops/${shopId}/papers/`, { method: 'POST', body: item })
  }
}
</script>
```

### Best practices

- **Papers bulk add**: One form per row; validate before submit; show inline errors.
- **Finishing list**: Same pattern; `charge_unit` options: PER_PIECE, PER_SIDE, PER_SHEET, PER_SQM, FLAT.
- **Printing rates grid**: Inline edit (click cell → input → blur to save) or modal per row.

---

Project is being built incrementally.
