"""Tests for entitlement enforcement across all four plan tiers."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from billing.models import Plan, ShopSubscription, SubscriptionShop
from billing.services.entitlements import (
    check_can_add_user,
    check_can_create_machine,
    check_can_create_product,
    check_can_create_quote,
    check_can_create_shop,
)
from billing.services.plans import seed_plans

User = get_user_model()


def make_user(email):
    return User.objects.create_user(email=email, password="testpass")


def make_subscription(owner, plan_code, status=ShopSubscription.STATUS_ACTIVE):
    plan = Plan.objects.get(code=plan_code)
    return ShopSubscription.objects.create(
        owner=owner,
        plan=plan,
        billing_interval=ShopSubscription.INTERVAL_MONTHLY,
        status=status,
    )


class FreeShopLimitTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("free@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_FREE)

    def _mock_shop_count(self, count):
        return patch("billing.services.entitlements._count_owner_shops", return_value=count)

    def test_free_allows_first_shop(self):
        with self._mock_shop_count(0):
            result = check_can_create_shop(self.owner)
        self.assertTrue(result["allowed"])

    def test_free_rejects_second_shop(self):
        with self._mock_shop_count(1):
            result = check_can_create_shop(self.owner)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "shop_limit_reached")

    def test_free_limit_is_1(self):
        self.assertEqual(Plan.objects.get(code=Plan.CODE_FREE).shops_limit, 1)


class BiasharaShopLimitTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("biashara@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA)

    def _mock_shop_count(self, count):
        return patch("billing.services.entitlements._count_owner_shops", return_value=count)

    def test_biashara_allows_first_shop(self):
        with self._mock_shop_count(0):
            result = check_can_create_shop(self.owner)
        self.assertTrue(result["allowed"])

    def test_biashara_rejects_second_shop(self):
        with self._mock_shop_count(1):
            result = check_can_create_shop(self.owner)
        self.assertFalse(result["allowed"])

    def test_biashara_limit_is_1(self):
        self.assertEqual(Plan.objects.get(code=Plan.CODE_BIASHARA).shops_limit, 1)


class BiasharaPlusShopLimitTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("plus@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA_PLUS)

    def _mock_shop_count(self, count):
        return patch("billing.services.entitlements._count_owner_shops", return_value=count)

    def test_plus_allows_two_shops(self):
        with self._mock_shop_count(1):
            result = check_can_create_shop(self.owner)
        self.assertTrue(result["allowed"])

    def test_plus_rejects_third_shop(self):
        with self._mock_shop_count(2):
            result = check_can_create_shop(self.owner)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["limit"], 2)

    def test_plus_limit_is_2(self):
        self.assertEqual(Plan.objects.get(code=Plan.CODE_BIASHARA_PLUS).shops_limit, 2)


class BiasharaMaxShopLimitTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("max@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA_MAX)

    def _mock_shop_count(self, count):
        return patch("billing.services.entitlements._count_owner_shops", return_value=count)

    def test_max_allows_five_shops(self):
        with self._mock_shop_count(4):
            result = check_can_create_shop(self.owner)
        self.assertTrue(result["allowed"])

    def test_max_rejects_sixth_shop(self):
        with self._mock_shop_count(5):
            result = check_can_create_shop(self.owner)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["limit"], 5)

    def test_max_limit_is_5(self):
        self.assertEqual(Plan.objects.get(code=Plan.CODE_BIASHARA_MAX).shops_limit, 5)


class FreeQuoteCapTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("quoter@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_FREE)

    def _mock_quote_count(self, count):
        return patch("billing.services.entitlements._count_quotes_this_month", return_value=count)

    def test_free_allows_under_limit(self):
        with self._mock_quote_count(14):
            result = check_can_create_quote(self.owner)
        self.assertTrue(result["allowed"])

    def test_free_caps_at_15(self):
        with self._mock_quote_count(15):
            result = check_can_create_quote(self.owner)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "quote_limit_reached")
        self.assertEqual(result["limit"], 15)

    def test_biashara_max_unlimited_quotes(self):
        owner2 = make_user("maxquoter@test.com")
        make_subscription(owner2, Plan.CODE_BIASHARA_MAX)
        with self._mock_quote_count(9999):
            result = check_can_create_quote(owner2)
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["limit"])


class SuspendedSubscriptionBlocksAllTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("suspended@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA, status=ShopSubscription.STATUS_SUSPENDED)

    def test_suspended_blocks_shop_creation(self):
        with patch("billing.services.entitlements._count_owner_shops", return_value=0):
            result = check_can_create_shop(self.owner)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "subscription_suspended")

    def test_suspended_blocks_quote_creation(self):
        with patch("billing.services.entitlements._count_quotes_this_month", return_value=0):
            result = check_can_create_quote(self.owner)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "subscription_suspended")
