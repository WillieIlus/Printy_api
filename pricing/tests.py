from decimal import Decimal

from django.test import TestCase, override_settings

from accounts.models import User, UserProfile
from jobs.payment_services import calculate_partner_job_split
from pricing.models import ShopPricingSettings
from services.pricing.marketplace_pricing import (
    apply_marketplace_pricing_to_preview,
    build_marketplace_pricing_summary,
    calculate_client_price,
)
from shops.models import Shop


class MarketplacePricingServiceTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="pricing-owner@test.com", password="pass12345")
        self.shop = Shop.objects.create(owner=self.owner, name="Pricing Shop", slug="pricing-shop", is_active=True)

    def test_calculate_client_price_uses_additive_default_margins(self):
        summary = calculate_client_price(Decimal("100.00"))
        self.assertEqual(summary["client_price"], Decimal("160.00"))
        self.assertEqual(summary["broker_margin_amount"], Decimal("30.00"))
        self.assertEqual(summary["service_margin_amount"], Decimal("30.00"))

    def test_calculate_client_price_supports_larger_amounts_without_compounding(self):
        summary = calculate_client_price(Decimal("5680.00"))
        self.assertEqual(summary["client_price"], Decimal("9088.00"))
        self.assertEqual(summary["broker_margin_amount"], Decimal("1704.00"))
        self.assertEqual(summary["service_margin_amount"], Decimal("1704.00"))

    def test_build_marketplace_pricing_summary_uses_shop_override_when_present(self):
        ShopPricingSettings.objects.create(
            shop=self.shop,
            broker_margin_percent=Decimal("10.00"),
            service_margin_percent=Decimal("20.00"),
        )
        summary = build_marketplace_pricing_summary(
            base_price=Decimal("100.00"),
            shop=self.shop,
            currency="KES",
        )
        self.assertEqual(summary["client_price"], "130.00")
        self.assertEqual(summary["broker_margin_amount"], "10.00")
        self.assertEqual(summary["service_margin_amount"], "20.00")

    def test_apply_marketplace_pricing_to_preview_falls_back_to_standard_defaults(self):
        preview = apply_marketplace_pricing_to_preview(
            {
                "currency": "KES",
                "totals": {
                    "subtotal": "100.00",
                    "grand_total": "100.00",
                },
                "breakdown": {},
            },
            shop=self.shop,
        )
        self.assertEqual(preview["totals"]["shop_total"], "100.00")
        self.assertEqual(preview["totals"]["grand_total"], "160.00")
        self.assertEqual(preview["marketplace_pricing"]["client_price"], "160.00")


@override_settings(PRINTY_PLATFORM_FEE_RATE=Decimal("0.30"))
class PartnerJobSplitTestCase(TestCase):
    def setUp(self):
        self.partner = User.objects.create_user(email="partner-split@test.com", password="pass12345", role="broker")
        self.profile = UserProfile.objects.create(user=self.partner, default_markup_rate=Decimal("0.30"))

    def test_partner_mediated_job_keeps_additive_partner_and_platform_fees(self):
        split = calculate_partner_job_split(Decimal("100.00"), partner_user=self.partner)
        self.assertEqual(split["production_amount"], Decimal("100.00"))
        self.assertEqual(split["broker_margin_amount"], Decimal("30.00"))
        self.assertEqual(split["platform_service_amount"], Decimal("30.00"))
        self.assertEqual(split["client_total"], Decimal("160.00"))

    def test_shop_owned_client_keeps_only_platform_fee(self):
        split = calculate_partner_job_split(
            Decimal("100.00"),
            broker_assigned=False,
            absorb_unused_broker_slot=False,
            shop_owns_client_directly=True,
            partner_user=self.partner,
        )
        self.assertEqual(split["broker_margin_amount"], Decimal("0.00"))
        self.assertEqual(split["platform_service_amount"], Decimal("30.00"))
        self.assertEqual(split["client_total"], Decimal("130.00"))

    def test_no_broker_assigned_rolls_unused_markup_into_platform_fee(self):
        split = calculate_partner_job_split(
            Decimal("100.00"),
            broker_assigned=False,
            partner_user=self.partner,
        )
        self.assertEqual(split["broker_margin_amount"], Decimal("0.00"))
        self.assertEqual(split["platform_service_percent"], Decimal("60.00"))
        self.assertEqual(split["platform_service_amount"], Decimal("60.00"))
        self.assertEqual(split["client_total"], Decimal("160.00"))
