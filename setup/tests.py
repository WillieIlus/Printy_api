from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from catalog.models import Product
from inventory.models import Machine, Paper
from pricing.models import FinishingRate, PrintingRate
from shops.models import Shop

from .services import get_setup_status, get_setup_status_for_shop, pricing_exists, get_product_publish_check


class SetupStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="printer@test.com", password="test1234", name="Test Printer")

    def test_no_shop_returns_shop_step(self):
        status = get_setup_status(self.user)
        self.assertFalse(status["has_shop"])
        self.assertEqual(status["next_step"], "shop")

    def test_shop_no_machines_returns_machines_step(self):
        Shop.objects.create(name="Test Shop", owner=self.user, currency="KES")
        status = get_setup_status(self.user)
        self.assertTrue(status["has_shop"])
        self.assertEqual(status["next_step"], "profile")

    def test_full_setup_returns_done(self):
        shop = Shop.objects.create(
            name="Test Shop",
            owner=self.user,
            currency="KES",
            description="Commercial print shop in Nairobi.",
            business_email="hello@testshop.com",
            phone_number="+254711111111",
            address_line="Kimathi Street",
            city="Nairobi",
            country="Kenya",
            is_public=True,
        )
        machine = Machine.objects.create(name="Konica", shop=shop, machine_type="DIGITAL", max_width_mm=320, max_height_mm=450)
        Paper.objects.create(shop=shop, sheet_size="SRA3", gsm=300, paper_type="GLOSS", buying_price=Decimal("15"), selling_price=Decimal("24"), width_mm=320, height_mm=450)
        PrintingRate.objects.create(machine=machine, sheet_size="SRA3", color_mode="COLOR", single_price=Decimal("45"), double_price=Decimal("75"))
        FinishingRate.objects.create(shop=shop, name="Gloss Lamination", slug="lamination", price=Decimal("12.00"))
        Product.objects.create(
            shop=shop,
            name="Business Card",
            pricing_mode="SHEET",
            default_finished_width_mm=90,
            default_finished_height_mm=54,
            status="PUBLISHED",
            standard_turnaround_hours=24,
        )
        status = get_setup_status(self.user)
        self.assertEqual(status["next_step"], "done")
        self.assertTrue(status["pricing_ready"])

    def test_shop_status_exposes_machine_and_paper_prerequisites(self):
        shop = Shop.objects.create(name="Prereq Shop", owner=self.user, slug="prereq-shop", currency="KES")

        status = get_setup_status_for_shop(shop)
        self.assertFalse(status["has_machines"])
        self.assertFalse(status["has_papers"])
        self.assertFalse(status["shop_profile_complete"])
        self.assertEqual(status["next_step"], "profile")
        self.assertTrue(status["steps"])
        self.assertEqual(status["steps"][0]["key"], "profile")
        self.assertFalse(status["steps"][0]["done"])
        self.assertTrue(status["steps"][1]["accessible"])
        self.assertEqual(status["steps"][0]["cta_label"], "Complete now")

        Machine.objects.create(name="Konica", shop=shop, machine_type="DIGITAL", max_width_mm=320, max_height_mm=450)
        shop.description = "Busy commercial printer."
        shop.business_email = "team@prereq-shop.com"
        shop.phone_number = "+254722222222"
        shop.address_line = "Moi Avenue"
        shop.save(update_fields=["description", "business_email", "phone_number", "address_line"])
        status = get_setup_status_for_shop(shop)
        self.assertTrue(status["has_machines"])
        self.assertEqual(status["next_step"], "materials")
        self.assertEqual(status["steps"][1]["key"], "materials")
        self.assertTrue(status["steps"][1]["accessible"])
        self.assertEqual(status["steps"][1]["cta_url"], "/dashboard/shop/materials")

    def test_rate_card_readiness_fields_are_exposed(self):
        shop = Shop.objects.create(
            name="Ready Shop",
            owner=self.user,
            slug="ready-shop",
            currency="KES",
            description="Offset and digital printing.",
            business_email="sales@ready-shop.com",
            phone_number="+254733333333",
            address_line="Westlands",
            city="Nairobi",
            country="Kenya",
            is_public=True,
        )
        machine = Machine.objects.create(name="Konica", shop=shop, machine_type="DIGITAL", max_width_mm=320, max_height_mm=450)
        Paper.objects.create(shop=shop, sheet_size="SRA3", gsm=300, paper_type="GLOSS", buying_price=Decimal("15"), selling_price=Decimal("24"), width_mm=320, height_mm=450)
        PrintingRate.objects.create(machine=machine, sheet_size="SRA3", color_mode="COLOR", single_price=Decimal("45"), double_price=Decimal("75"))

        status = get_setup_status_for_shop(shop)
        self.assertTrue(status["shop_profile_complete"])
        self.assertTrue(status["has_materials"])
        self.assertEqual(status["materials_count"], 1)
        self.assertTrue(status["has_pricing_rules"])
        self.assertEqual(status["pricing_rules_count"], 1)
        self.assertFalse(status["has_finishing_rates"])
        self.assertFalse(status["turnaround_configured"])
        self.assertTrue(status["shop_published"])
        self.assertTrue(status["can_receive_requests"])
        self.assertTrue(status["can_price_requests"])
        self.assertEqual(status["rate_card_completeness"], 70)
        self.assertIn("finishing rates", " ".join(status["warnings"]).lower())


class PricingExistsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="p2@test.com", password="test1234", name="P2")
        self.shop = Shop.objects.create(name="Shop2", owner=self.user, currency="KES")

    def test_no_pricing(self):
        self.assertFalse(pricing_exists(self.shop))

    def test_machine_and_paper_and_rate(self):
        machine = Machine.objects.create(name="M1", shop=self.shop, machine_type="DIGITAL", max_width_mm=320, max_height_mm=450)
        Paper.objects.create(shop=self.shop, sheet_size="SRA3", gsm=300, paper_type="GLOSS", buying_price=Decimal("15"), selling_price=Decimal("24"), width_mm=320, height_mm=450)
        PrintingRate.objects.create(machine=machine, sheet_size="SRA3", color_mode="COLOR", single_price=Decimal("45"), double_price=Decimal("75"))
        self.assertTrue(pricing_exists(self.shop))


class PublishRulesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="p3@test.com", password="test1234", name="P3")
        self.shop = Shop.objects.create(name="Shop3", owner=self.user, currency="KES")

    def test_cannot_publish_without_pricing(self):
        product = Product.objects.create(shop=self.shop, name="Test", pricing_mode="SHEET", default_finished_width_mm=90, default_finished_height_mm=54)
        check = get_product_publish_check(product)
        self.assertFalse(check["can_publish"])
        self.assertTrue(any("printing rates" in r.lower() or "machine" in r.lower() for r in check["block_reasons"]))

    def test_can_publish_with_pricing(self):
        machine = Machine.objects.create(name="M1", shop=self.shop, machine_type="DIGITAL", max_width_mm=320, max_height_mm=450)
        Paper.objects.create(shop=self.shop, sheet_size="SRA3", gsm=300, paper_type="GLOSS", buying_price=Decimal("15"), selling_price=Decimal("24"), width_mm=320, height_mm=450)
        PrintingRate.objects.create(machine=machine, sheet_size="SRA3", color_mode="COLOR", single_price=Decimal("45"), double_price=Decimal("75"))
        product = Product.objects.create(shop=self.shop, name="Business Card", pricing_mode="SHEET", default_finished_width_mm=90, default_finished_height_mm=54)
        check = get_product_publish_check(product)
        self.assertTrue(check["can_publish"])
