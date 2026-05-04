"""
Location-based market pricing aggregation service.

Queries shops with pricing_ranges set, aggregates median + mean per product
for a given city/area, with fallback to city-wide data when local sample < 5.
"""
import statistics
from .models import Shop

PRODUCT_SPECS = {
    "booklets": "50 units, A5, 300gsm, stapled",
    "flyers": "500 units, A5, 150gsm, uncut",
    "posters": "10 units, A2, 200gsm, uncut",
    "business_cards": "500 units, 250gsm, unlaminated",
}

PRODUCT_LABELS = {
    "booklets": "Booklets",
    "flyers": "A4 Flyers",
    "posters": "Posters",
    "business_cards": "Business Cards",
}

MIN_SAMPLE = 5


def _compute_product_pricing(shops, product: str) -> dict | None:
    low_prices, high_prices = [], []
    for shop in shops:
        if not shop.pricing_ranges or product not in shop.pricing_ranges:
            continue
        pr = shop.pricing_ranges[product]
        if not isinstance(pr, dict):
            continue
        try:
            low_prices.append(float(pr["low"]))
            high_prices.append(float(pr["high"]))
        except (KeyError, TypeError, ValueError):
            pass

    if not low_prices or not high_prices:
        return None

    return {
        "market_range": {
            "low": round(min(low_prices)),
            "high": round(max(high_prices)),
        },
        "median": round((statistics.median(low_prices) + statistics.median(high_prices)) / 2),
        "mean": round((statistics.mean(low_prices) + statistics.mean(high_prices)) / 2),
        "shops_contributing": len(low_prices),
    }


def get_location_pricing(location: str, fallback_to_city: bool = True) -> dict:
    """
    Returns aggregated market pricing for the given location.

    Args:
        location: area name, e.g. "Nairobi CBD" or "Nairobi"
        fallback_to_city: expand to city-level if local sample < MIN_SAMPLE

    Returns:
        dict matching the /api/shops/location-pricing/ response schema
    """
    base_qs = Shop.objects.filter(
        is_active=True,
        pricing_ranges__isnull=False,
    )

    local_shops = base_qs.filter(city__iexact=location)
    shops_in_area = local_shops.count()

    active_shops = local_shops
    fallback_used = False
    fallback_location = None

    if shops_in_area < MIN_SAMPLE and fallback_to_city:
        city = location.split()[0]
        city_shops = base_qs.filter(city__icontains=city)
        if city_shops.count() > 0 and city_shops.count() > shops_in_area:
            active_shops = city_shops
            fallback_used = True
            fallback_location = city

    pricing_data = {}
    for product in PRODUCT_SPECS:
        result = _compute_product_pricing(active_shops, product)
        if result:
            pricing_data[product] = result

    shops_count = active_shops.count()

    warning = None
    if fallback_used:
        warning = (
            f"Only {shops_in_area} shop{'s' if shops_in_area != 1 else ''} found in {location}. "
            f"Showing {fallback_location}-wide data instead."
        )

    return {
        "location": fallback_location or location,
        "shops_in_location": shops_count,
        "pricing_data": pricing_data,
        "sufficient_data": shops_count >= MIN_SAMPLE,
        "warning": warning,
        "fallback_location": fallback_location,
        "fallback_used": fallback_used,
    }
