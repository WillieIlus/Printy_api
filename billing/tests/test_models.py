"""Tests for billing models and plan seeding."""
from decimal import Decimal

from django.test import TestCase

from billing.models import Plan, ShopSubscription
from billing.services.plans import seed_plans, get_free_plan


class PlanSeedingTest(TestCase):
    def setUp(self):
        seed_plans()

    def test_four_plans_seeded(self):
        self.assertEqual(Plan.objects.count(), 4)

    def test_free_plan_values(self):
        plan = Plan.objects.get(code=Plan.CODE_FREE)
        self.assertEqual(plan.name, "Free")
        self.assertEqual(plan.price_monthly, Decimal("0.00"))
        self.assertEqual(plan.price_annual, Decimal("0.00"))
        self.assertEqual(plan.shops_limit, 1)
        self.assertEqual(plan.machines_limit, 1)
        self.assertEqual(plan.products_limit, 3)
        self.assertEqual(plan.quotes_per_month_limit, 15)
        self.assertEqual(plan.users_limit, 1)
        self.assertFalse(plan.branded_quotes_enabled)
        self.assertFalse(plan.customer_history_enabled)
        self.assertEqual(plan.analytics_level, Plan.ANALYTICS_BASIC)
        self.assertFalse(plan.priority_support)
        self.assertTrue(plan.is_free)

    def test_biashara_plan_values(self):
        plan = Plan.objects.get(code=Plan.CODE_BIASHARA)
        self.assertEqual(plan.name, "Biashara")
        self.assertEqual(plan.price_monthly, Decimal("1500.00"))
        self.assertEqual(plan.shops_limit, 1)
        self.assertEqual(plan.machines_limit, 3)
        self.assertEqual(plan.products_limit, 15)
        self.assertEqual(plan.quotes_per_month_limit, 100)
        self.assertEqual(plan.users_limit, 2)
        self.assertTrue(plan.branded_quotes_enabled)
        self.assertTrue(plan.customer_history_enabled)

    def test_biashara_plus_plan_values(self):
        plan = Plan.objects.get(code=Plan.CODE_BIASHARA_PLUS)
        self.assertEqual(plan.name, "Biashara Plus")
        self.assertEqual(plan.shops_limit, 2)
        self.assertEqual(plan.machines_limit, 10)
        self.assertEqual(plan.products_limit, 50)
        self.assertEqual(plan.quotes_per_month_limit, 400)
        self.assertEqual(plan.users_limit, 5)

    def test_biashara_max_plan_values(self):
        plan = Plan.objects.get(code=Plan.CODE_BIASHARA_MAX)
        self.assertEqual(plan.name, "Biashara Max")
        self.assertEqual(plan.shops_limit, 5)
        self.assertIsNone(plan.machines_limit)
        self.assertIsNone(plan.products_limit)
        self.assertIsNone(plan.quotes_per_month_limit)
        self.assertEqual(plan.users_limit, 15)
        self.assertTrue(plan.priority_support)
        self.assertTrue(plan.is_unlimited("machines_limit"))
        self.assertTrue(plan.is_unlimited("products_limit"))

    def test_seed_is_idempotent(self):
        seed_plans()
        self.assertEqual(Plan.objects.count(), 4)

    def test_plan_get_price(self):
        plan = Plan.objects.get(code=Plan.CODE_BIASHARA)
        self.assertEqual(plan.get_price("monthly"), Decimal("1500.00"))
        self.assertEqual(plan.get_price("annual"), Decimal("15000.00"))
