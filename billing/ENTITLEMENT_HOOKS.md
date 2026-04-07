# Entitlement Hook Locations

These are the exact places to add `billing.services.entitlements` checks
into existing endpoints.  Each snippet shows the guard pattern only —
paste it at the top of the relevant `create()` / `perform_create()` method.

---

## 1. Shop creation  →  `api/views.py` — `ShopViewSet.perform_create`

```python
# In ShopViewSet.perform_create (or create):
from billing.services.entitlements import check_can_create_shop
from rest_framework.exceptions import PermissionDenied

result = check_can_create_shop(self.request.user)
if not result["allowed"]:
    raise PermissionDenied(result["message"])
```

---

## 2. Machine creation  →  `api/views.py` — `ShopMachineViewSet.perform_create`

```python
from billing.services.entitlements import check_can_create_machine
from rest_framework.exceptions import PermissionDenied

shop = get_object_or_404(Shop, ...)
result = check_can_create_machine(shop)
if not result["allowed"]:
    raise PermissionDenied(result["message"])
```

---

## 3. Product creation  →  `gallery/views.py` — `GalleryProductViewSet.perform_create`

```python
from billing.services.entitlements import check_can_create_product
from rest_framework.exceptions import PermissionDenied

result = check_can_create_product(self.shop)
if not result["allowed"]:
    raise PermissionDenied(result["message"])
```

Also hook `api/views.py` → `ShopProductViewSet.perform_create` the same way.

---

## 4. Quote creation  →  `api/views.py` / `api/quote_views.py` — quote create paths

Both `CustomerQuoteRequestViewSet.perform_create` and
`QuoteViewSet.perform_create` (staff quoting) should guard:

```python
from billing.services.entitlements import check_can_create_quote
from rest_framework.exceptions import PermissionDenied

# owner = the shop owner, not necessarily request.user
owner = shop.owner
result = check_can_create_quote(owner)
if not result["allowed"]:
    raise PermissionDenied(result["message"])
```

For `ShopQuote` creation in `workflow_views.py → QuoteResponseListCreateView`:

```python
from billing.services.entitlements import check_can_create_quote

result = check_can_create_quote(shop.owner)
if not result["allowed"]:
    raise PermissionDenied(result["message"])
```

---

## 5. Team member (ShopMembership) invitation

Wherever `ShopMembership.objects.create(...)` is called (setup app or
any membership invite endpoint):

```python
from billing.services.entitlements import check_can_add_user
from rest_framework.exceptions import PermissionDenied

result = check_can_add_user(shop)
if not result["allowed"]:
    raise PermissionDenied(result["message"])
```

---

## Response shape

Every entitlement check returns:

```json
{
  "allowed": true | false,
  "reason_code": "ok" | "shop_limit_reached" | "machine_limit_reached" | "product_limit_reached" | "quote_limit_reached" | "user_limit_reached" | "subscription_suspended" | "no_subscription",
  "message": "Human-readable explanation",
  "current": 2,
  "limit": 3
}
```

Pass `message` directly to `PermissionDenied` — it surfaces in the 403 response body.

---

## Branded quotes feature flag check

```python
from billing.selectors import get_active_subscription_for_owner

sub = get_active_subscription_for_owner(shop.owner)
if not sub.plan.branded_quotes_enabled:
    raise PermissionDenied("Branded quotes require the Biashara plan or above.")
```

---

## Customer history feature flag check

```python
sub = get_active_subscription_for_owner(shop.owner)
if not sub.plan.customer_history_enabled:
    raise PermissionDenied("Customer history requires the Biashara plan or above.")
```
