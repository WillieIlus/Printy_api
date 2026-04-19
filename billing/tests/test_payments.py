"""Tests for M-Pesa payment callbacks, idempotency, and subscription activation."""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import Plan, PaymentTransaction, RenewalAttempt, ShopSubscription
from billing.services.callbacks import handle_mpesa_callback
from billing.services.payments import initiate_test_stk_push, normalize_phone_number, parse_callback
from billing.services.plans import seed_plans

User = get_user_model()


def make_user(email):
    return User.objects.create_user(email=email, password="testpass")


def make_subscription(owner, plan_code, status=ShopSubscription.STATUS_TRIALING):
    plan = Plan.objects.get(code=plan_code)
    return ShopSubscription.objects.create(
        owner=owner,
        plan=plan,
        billing_interval=ShopSubscription.INTERVAL_MONTHLY,
        status=status,
        payment_phone_e164="254700000001",
    )


def make_txn(owner, sub, status=PaymentTransaction.STATUS_PROCESSING, checkout_id="CHK001"):
    return PaymentTransaction.objects.create(
        subscription=sub,
        owner=owner,
        plan=sub.plan,
        transaction_type=PaymentTransaction.TYPE_ACTIVATION,
        amount=Decimal("1500.00"),
        currency="KES",
        status=status,
        checkout_request_id=checkout_id,
        merchant_request_id="MER001",
        phone_number="254700000001",
        idempotency_key=f"test-{checkout_id}",
    )


SAMPLE_SUCCESS_CALLBACK = {
    "Body": {
        "stkCallback": {
            "MerchantRequestID": "MER001",
            "CheckoutRequestID": "CHK001",
            "ResultCode": 0,
            "ResultDesc": "The service request is processed successfully.",
            "CallbackMetadata": {
                "Item": [
                    {"Name": "Amount", "Value": 1500},
                    {"Name": "MpesaReceiptNumber", "Value": "RCP12345"},
                    {"Name": "TransactionDate", "Value": 20241201120000},
                    {"Name": "PhoneNumber", "Value": 254700000001},
                ]
            },
        }
    }
}

SAMPLE_FAILURE_CALLBACK = {
    "Body": {
        "stkCallback": {
            "MerchantRequestID": "MER001",
            "CheckoutRequestID": "CHK001",
            "ResultCode": 1032,
            "ResultDesc": "Request cancelled by user.",
        }
    }
}


class PhoneNormalizationTest(TestCase):
    def test_07xx_format(self):
        self.assertEqual(normalize_phone_number("0712345678"), "254712345678")

    def test_254_format(self):
        self.assertEqual(normalize_phone_number("254712345678"), "254712345678")

    def test_plus_254_format(self):
        self.assertEqual(normalize_phone_number("+254712345678"), "254712345678")

    def test_7xx_format(self):
        self.assertEqual(normalize_phone_number("712345678"), "254712345678")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            normalize_phone_number("123")


class CallbackParsingTest(TestCase):
    def test_parse_success_callback(self):
        parsed = parse_callback(SAMPLE_SUCCESS_CALLBACK)
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["result_code"], "0")
        self.assertEqual(parsed["mpesa_receipt_number"], "RCP12345")
        self.assertEqual(parsed["amount"], Decimal("1500"))
        self.assertEqual(parsed["checkout_request_id"], "CHK001")

    def test_parse_failure_callback(self):
        parsed = parse_callback(SAMPLE_FAILURE_CALLBACK)
        self.assertFalse(parsed["success"])
        self.assertEqual(parsed["result_code"], "1032")
        self.assertIsNone(parsed["mpesa_receipt_number"])


class SuccessfulCallbackActivatesSubscriptionTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("callback@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA)
        self.txn = make_txn(self.owner, self.sub)

    def test_success_callback_activates_subscription(self):
        result = handle_mpesa_callback(SAMPLE_SUCCESS_CALLBACK)
        self.assertEqual(result["status"], "ok")

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, PaymentTransaction.STATUS_SUCCESS)
        self.assertEqual(self.txn.mpesa_receipt_number, "RCP12345")

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ShopSubscription.STATUS_ACTIVE)
        self.assertIsNotNone(self.sub.ends_at)

    def test_failed_callback_keeps_subscription_in_trialing(self):
        failure = dict(SAMPLE_FAILURE_CALLBACK)
        result = handle_mpesa_callback(failure)
        self.assertEqual(result["status"], "ok")

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, PaymentTransaction.STATUS_CANCELLED)

        self.sub.refresh_from_db()
        # Subscription stays in trialing after first failure (no grace yet — requires exhausted retries)
        self.assertNotEqual(self.sub.status, ShopSubscription.STATUS_ACTIVE)


class DuplicateCallbackIdempotencyTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("dupe@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA)
        self.txn = make_txn(self.owner, self.sub, status=PaymentTransaction.STATUS_SUCCESS)
        self.txn.mpesa_receipt_number = "RCP12345"
        self.txn.save()

    def test_duplicate_callback_does_not_re_activate(self):
        original_ends_at = self.sub.ends_at
        result = handle_mpesa_callback(SAMPLE_SUCCESS_CALLBACK)
        self.assertEqual(result["status"], "ok")
        self.assertIn("Already processed", result["message"])

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.ends_at, original_ends_at)


class DuplicateReceiptRejectedTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("rcpt@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA)
        # A different transaction has already consumed the receipt
        other_owner = make_user("other@test.com")
        other_sub = make_subscription(other_owner, Plan.CODE_BIASHARA)
        PaymentTransaction.objects.create(
            subscription=other_sub,
            owner=other_owner,
            plan=other_sub.plan,
            transaction_type=PaymentTransaction.TYPE_ACTIVATION,
            amount=Decimal("1500"),
            currency="KES",
            status=PaymentTransaction.STATUS_SUCCESS,
            mpesa_receipt_number="RCP12345",
            idempotency_key="other-unique-key",
        )
        self.txn = make_txn(self.owner, self.sub, checkout_id="CHK001")

    def test_duplicate_receipt_is_rejected(self):
        result = handle_mpesa_callback(SAMPLE_SUCCESS_CALLBACK)
        self.assertEqual(result["status"], "ok")
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, PaymentTransaction.STATUS_FAILED)


class RenewalFailureGracePeriodTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("renew@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA, status=ShopSubscription.STATUS_ACTIVE)

    def test_exhausted_retries_trigger_grace_period(self):
        from billing.services.renewals import _escalate_to_past_due_if_exhausted, RETRY_SCHEDULE_HOURS
        attempt = RenewalAttempt.objects.create(
            subscription=self.sub,
            due_at=timezone.now(),
            attempt_number=len(RETRY_SCHEDULE_HOURS) + 1,  # exhausted
            status=RenewalAttempt.STATUS_FAILED,
        )
        _escalate_to_past_due_if_exhausted(self.sub, attempt)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ShopSubscription.STATUS_GRACE)
        self.assertIsNotNone(self.sub.grace_period_ends_at)

    def test_grace_expiry_suspends_subscription(self):
        from billing.services.subscriptions import suspend_if_grace_expired
        from datetime import timedelta

        self.sub.status = ShopSubscription.STATUS_GRACE
        self.sub.grace_period_ends_at = timezone.now() - timedelta(seconds=1)
        self.sub.save()

        result = suspend_if_grace_expired(self.sub)
        self.assertTrue(result)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ShopSubscription.STATUS_SUSPENDED)


class DowngradePreservesDataTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("downgrade@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA_PLUS, status=ShopSubscription.STATUS_ACTIVE)

    def test_downgrade_does_not_delete_subscription(self):
        from billing.services.subscriptions import request_downgrade
        sub = request_downgrade(owner=self.owner, target_plan_code=Plan.CODE_BIASHARA)
        # Subscription still exists and is still active (cancellation is at period end)
        self.assertEqual(sub.status, ShopSubscription.STATUS_ACTIVE)
        self.assertIsNotNone(sub.cancellation_requested_at)
        # Plan has NOT changed yet
        self.assertEqual(sub.plan.code, Plan.CODE_BIASHARA_PLUS)


class OverLimitFlagTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("overlimit@test.com")
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA, status=ShopSubscription.STATUS_ACTIVE)

    def test_over_limit_computed_correctly(self):
        from billing.services.subscriptions import _compute_over_limit
        # Mock usage exceeding plan limits
        with patch("billing.services.entitlements.get_current_usage") as mock_usage, \
             patch("billing.services.entitlements.get_plan_limits") as mock_limits:
            mock_usage.return_value = {"shops": 2, "machines": 4, "active_products": 5, "team_members": 3, "quotes_this_month": 50}
            mock_limits.return_value = {"shops_limit": 1, "machines_limit": 3, "products_limit": 15, "quotes_per_month_limit": 100, "users_limit": 2}
            result = _compute_over_limit(self.sub)
        self.assertTrue(result)


class SuccessfulCallbackReplacesFreeSubscriptionTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("replace-free@test.com")
        self.free_sub = ShopSubscription.objects.create(
            owner=self.owner,
            plan=Plan.objects.get(code=Plan.CODE_FREE),
            billing_interval=ShopSubscription.INTERVAL_MONTHLY,
            status=ShopSubscription.STATUS_ACTIVE,
        )
        self.paid_sub = make_subscription(self.owner, Plan.CODE_BIASHARA)
        self.txn = make_txn(self.owner, self.paid_sub)

    def test_success_callback_expires_previous_current_subscription(self):
        result = handle_mpesa_callback(SAMPLE_SUCCESS_CALLBACK)
        self.assertEqual(result["status"], "ok")

        self.paid_sub.refresh_from_db()
        self.free_sub.refresh_from_db()

        self.assertEqual(self.paid_sub.status, ShopSubscription.STATUS_ACTIVE)
        self.assertEqual(self.free_sub.status, ShopSubscription.STATUS_EXPIRED)
        self.assertFalse(self.free_sub.auto_renew_enabled)


class InitiateStkPushServiceTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("service@test.com")
        self.plan = Plan.objects.get(code=Plan.CODE_BIASHARA)
        self.sub = make_subscription(self.owner, Plan.CODE_BIASHARA)

    @patch("billing.services.payments.requests.post")
    @patch("billing.services.payments.get_mpesa_token")
    @patch("billing.services.payments._get_mpesa_config")
    def test_initiate_stk_push_persists_request_and_response(
        self,
        mock_config,
        mock_token,
        mock_post,
    ):
        from billing.services.payments import initiate_stk_push

        mock_config.return_value = {
            "base_url": "https://sandbox.safaricom.co.ke",
            "callback_url": "https://example.com/api/billing/mpesa/callback/",
            "consumer_key": "key",
            "consumer_secret": "secret",
            "shortcode": "174379",
            "passkey": "passkey",
            "env_name": "sandbox",
        }
        mock_token.return_value = "token"

        response = MagicMock()
        response.json.return_value = {
            "MerchantRequestID": "MER123",
            "CheckoutRequestID": "CHK123",
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing",
            "CustomerMessage": "Success",
        }
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        txn = initiate_stk_push(
            owner=self.owner,
            subscription=self.sub,
            plan=self.plan,
            phone_number="0712345678",
            amount=Decimal("1500.00"),
            transaction_type=PaymentTransaction.TYPE_ACTIVATION,
            idempotency_key="service-test-idem",
        )

        self.assertEqual(txn.status, PaymentTransaction.STATUS_PROCESSING)
        self.assertEqual(txn.response_code, "0")
        self.assertEqual(txn.checkout_request_id, "CHK123")
        self.assertEqual(txn.phone_number, "254712345678")
        self.assertIsNotNone(txn.raw_request)
        self.assertEqual(txn.raw_response["CheckoutRequestID"], "CHK123")


class SandboxTestStkServiceTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("sandbox-service@test.com")

    @override_settings(
        MPESA_ENV="sandbox",
        MPESA_BASE_URL="https://sandbox.safaricom.co.ke",
        MPESA_CALLBACK_URL="https://api.printy.ke/api/payments/mpesa/callback/",
        MPESA_CONSUMER_KEY="key",
        MPESA_CONSUMER_SECRET="secret",
        MPESA_SHORTCODE="174379",
        MPESA_PASSKEY="passkey",
        MPESA_ACCOUNT_REFERENCE_DEFAULT="Printyke",
    )
    @patch("billing.services.payments.requests.post")
    @patch("billing.services.payments.get_mpesa_token")
    def test_initiate_test_stk_push_forces_amount_one(self, mock_token, mock_post):
        mock_token.return_value = "token"

        response = MagicMock()
        response.json.return_value = {
            "MerchantRequestID": "MERTEST1",
            "CheckoutRequestID": "CHKTEST1",
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing",
            "CustomerMessage": "Success",
        }
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        txn = initiate_test_stk_push(owner=self.owner, phone_number="0712345678")

        self.assertEqual(txn.transaction_type, PaymentTransaction.TYPE_SANDBOX_TEST)
        self.assertEqual(txn.amount, Decimal("1.00"))
        self.assertEqual(txn.phone_number, "254712345678")
        self.assertEqual(txn.status, PaymentTransaction.STATUS_PROCESSING)
        self.assertEqual(txn.raw_request["Amount"], 1)

    @override_settings(
        MPESA_ENV="production",
        MPESA_BASE_URL="https://api.safaricom.co.ke",
        MPESA_CALLBACK_URL="https://api.printy.ke/api/payments/mpesa/callback/",
        MPESA_CONSUMER_KEY="key",
        MPESA_CONSUMER_SECRET="secret",
        MPESA_SHORTCODE="174379",
        MPESA_PASSKEY="passkey",
    )
    def test_initiate_test_stk_push_rejects_non_sandbox_env(self):
        with self.assertRaisesMessage(ValueError, "only available when MPESA_ENV=sandbox"):
            initiate_test_stk_push(owner=self.owner, phone_number="0712345678")


class SandboxTestCallbackTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("sandbox-callback@test.com")
        self.txn = PaymentTransaction.objects.create(
            owner=self.owner,
            subscription=None,
            plan=None,
            transaction_type=PaymentTransaction.TYPE_SANDBOX_TEST,
            amount=Decimal("1.00"),
            currency="KES",
            status=PaymentTransaction.STATUS_PROCESSING,
            checkout_request_id="CHK001",
            merchant_request_id="MER001",
            phone_number="254700000001",
            idempotency_key="sandbox-callback-1",
        )

    def test_success_callback_does_not_activate_subscription(self):
        result = handle_mpesa_callback(SAMPLE_SUCCESS_CALLBACK)
        self.assertEqual(result["status"], "ok")

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, PaymentTransaction.STATUS_SUCCESS)
        self.assertEqual(self.txn.mpesa_receipt_number, "RCP12345")
        self.assertEqual(ShopSubscription.objects.filter(owner=self.owner).count(), 0)


class SandboxTestEndpointTest(TestCase):
    def setUp(self):
        seed_plans()
        self.owner = make_user("sandbox-endpoint@test.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    @override_settings(
        MPESA_ENV="sandbox",
        MPESA_BASE_URL="https://sandbox.safaricom.co.ke",
        MPESA_CALLBACK_URL="https://api.printy.ke/api/payments/mpesa/callback/",
        MPESA_CONSUMER_KEY="key",
        MPESA_CONSUMER_SECRET="secret",
        MPESA_SHORTCODE="174379",
        MPESA_PASSKEY="passkey",
    )
    @patch("billing.services.payments.requests.post")
    @patch("billing.services.payments.get_mpesa_token")
    def test_authenticated_test_endpoint_creates_sandbox_transaction(self, mock_token, mock_post):
        mock_token.return_value = "token"

        response = MagicMock()
        response.json.return_value = {
            "MerchantRequestID": "MERTEST2",
            "CheckoutRequestID": "CHKTEST2",
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing",
            "CustomerMessage": "Success",
        }
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        resp = self.client.post(
            "/api/payments/mpesa/test-stk/",
            {"phone_number": "0712345678", "amount": "99.00"},
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertIn("Amount is fixed at KES 1", resp.data["message"])
        self.assertEqual(resp.data["transaction"]["amount"], "1.00")
        self.assertEqual(resp.data["transaction"]["transaction_type"], PaymentTransaction.TYPE_SANDBOX_TEST)

    def test_test_endpoint_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(
            "/api/payments/mpesa/test-stk/",
            {"phone_number": "0712345678"},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)
