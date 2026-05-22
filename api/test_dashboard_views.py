from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from api.models import PartnerClient
from jobs.models import JobAssignment, JobPayment, ManagedJob
from api.services.admin_dashboard import calculate_change, get_time_window_comparison
from billing.models import PaymentTransaction, Plan
from shops.models import Shop
from quotes.models import QuoteRequest


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DashboardHomeViewRoleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(
            email="client-dashboard@test.com",
            password="pass12345",
            name="Client Dashboard",
            role=User.Role.CLIENT,
        )
        cls.partner_user = User.objects.create_user(
            email="partner-dashboard@test.com",
            password="pass12345",
            name="Partner Dashboard",
            role=User.Role.PARTNER,
            partner_profile_enabled=True,
        )
        cls.production_user = User.objects.create_user(
            email="production-dashboard@test.com",
            password="pass12345",
            name="Production Dashboard",
            role=User.Role.PRODUCTION,
        )
        cls.shop = Shop.objects.create(
            name="Dashboard Print Shop",
            slug="dashboard-print-shop",
            owner=cls.production_user,
        )
        cls.managed_job = ManagedJob.objects.create(
            title="A4 Flyer Run",
            client=cls.client_user,
            created_by=cls.client_user,
            broker=cls.partner_user,
            assigned_shop=cls.shop,
            client_total="9088.00",
            production_total="7000.00",
            broker_commission="900.00",
            payment_status="pending",
            assignment_status="assigned",
            status="accepted",
        )
        JobAssignment.objects.create(
            managed_job=cls.managed_job,
            assigned_shop=cls.shop,
            status="pending",
            operational_priority_level=3,
        )

    def setUp(self):
        self.client = APIClient()

    def test_client_dashboard_returns_only_client_safe_payload(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.get("/api/dashboard/client-home/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["role"], "client")
        self.assertEqual(payload["recent_jobs"][0]["assigned_shop_name"], "Verified Print Partner")
        self.assertEqual(payload["recent_jobs"][0]["client_total"], "9088.00")

    def test_partner_dashboard_exposes_client_price_but_production_does_not(self):
        self.client.force_authenticate(user=self.partner_user)

        partner_response = self.client.get("/api/dashboard/partner-home/")

        self.assertEqual(partner_response.status_code, 200)
        self.assertEqual(partner_response.json()["recent_jobs"][0]["client_total"], "9088.00")

        self.client.force_authenticate(user=self.production_user)
        production_response = self.client.get("/api/dashboard/production-home/")

        self.assertEqual(production_response.status_code, 200)
        queue_row = production_response.json()["queue"][0]
        self.assertNotIn("client_total", queue_row)

    def test_dashboard_role_guards_block_cross_workspace_access(self):
        self.client.force_authenticate(user=self.partner_user)

        response = self.client.get("/api/dashboard/production-home/")

        self.assertEqual(response.status_code, 403)

    def test_client_cannot_access_partner_dashboard(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.get("/api/dashboard/partner-home/")

        self.assertEqual(response.status_code, 403)

    def test_partner_can_access_partner_dashboard(self):
        self.client.force_authenticate(user=self.partner_user)

        response = self.client.get("/api/dashboard/partner-home/")

        self.assertEqual(response.status_code, 200)

    def test_production_can_access_production_dashboard(self):
        self.client.force_authenticate(user=self.production_user)

        response = self.client.get("/api/dashboard/production-home/")

        self.assertEqual(response.status_code, 200)

    def test_role_specific_detail_endpoints_keep_pricing_visibility_safe(self):
        self.client.force_authenticate(user=self.client_user)
        client_response = self.client.get(f"/api/dashboard/client/jobs/{self.managed_job.id}/")
        self.assertEqual(client_response.status_code, 200)
        self.assertEqual(client_response.json()["job"]["pricing"]["client_total"], "9088.00")
        self.assertNotIn("production_total", client_response.json()["job"]["pricing"])
        self.assertEqual(client_response.json()["job"]["tracking_token"], str(self.managed_job.tracking_token))
        self.assertIsNone(client_response.json()["job"]["public_token"])
        self.assertIsNone(client_response.json()["settlement"])

        self.client.force_authenticate(user=self.partner_user)
        partner_response = self.client.get(f"/api/dashboard/partner/jobs/{self.managed_job.id}/")
        self.assertEqual(partner_response.status_code, 200)
        self.assertEqual(partner_response.json()["job"]["pricing"]["client_total"], "9088.00")
        self.assertEqual(partner_response.json()["job"]["pricing"]["production_total"], str(self.managed_job.production_total))

        self.client.force_authenticate(user=self.production_user)
        production_response = self.client.get(f"/api/dashboard/production/jobs/{self.managed_job.id}/")
        self.assertEqual(production_response.status_code, 200)
        self.assertEqual(production_response.json()["job"]["pricing"]["production_total"], str(self.managed_job.production_total))
        self.assertIsNone(production_response.json()["job"]["pricing"]["client_total"])

    def test_legacy_shop_route_target_has_new_production_jobs_api(self):
        self.client.force_authenticate(user=self.production_user)
        response = self.client.get("/api/dashboard/production/jobs/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "production")

    def test_unrelated_client_cannot_access_other_clients_tracking_token(self):
        other_client = User.objects.create_user(
            email="second-client-dashboard@test.com",
            password="pass12345",
            role=User.Role.CLIENT,
        )
        self.client.force_authenticate(user=other_client)

        response = self.client.get(f"/api/dashboard/client/jobs/{self.managed_job.id}/")

        self.assertEqual(response.status_code, 404)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminDashboardViewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_admin = User.objects.create_superuser(
            email="admin-dashboard@test.com",
            password="pass12345",
        )
        cls.client_user = User.objects.create_user(
            email="client-admin-dashboard@test.com",
            password="pass12345",
            role=User.Role.CLIENT,
        )
        cls.partner_user = User.objects.create_user(
            email="partner-admin-dashboard@test.com",
            password="pass12345",
            role=User.Role.PARTNER,
            partner_profile_enabled=True,
        )
        cls.production_user = User.objects.create_user(
            email="production-admin-dashboard@test.com",
            password="pass12345",
            role=User.Role.PRODUCTION,
        )
        cls.shop = Shop.objects.create(name="Admin Dashboard Shop", slug="admin-dashboard-shop", owner=cls.production_user)
        cls.quote_request = QuoteRequest.objects.create(
            shop=cls.shop,
            created_by=cls.client_user,
            customer_name="Admin Client",
            status=QuoteRequest.SUBMITTED,
        )
        cls.managed_job = ManagedJob.objects.create(
            title="Admin Job",
            source_quote_request=cls.quote_request,
            client=cls.client_user,
            broker=cls.partner_user,
            assigned_shop=cls.shop,
            status="completed",
            payment_status="confirmed",
            client_total="1200.00",
            production_total="800.00",
            platform_fee="120.00",
            broker_commission="80.00",
            completed_at=timezone.now(),
        )
        JobAssignment.objects.create(
            managed_job=cls.managed_job,
            assigned_shop=cls.shop,
            status="completed",
        )
        JobPayment.objects.create(
            managed_job=cls.managed_job,
            payer=cls.client_user,
            amount="1200.00",
            received_amount="1200.00",
            payment_status="paid",
            account_reference="MJ-1",
            confirmed_at=timezone.now(),
        )
        plan, _ = Plan.objects.get_or_create(code="FREE", defaults={"name": "Free"})
        PaymentTransaction.objects.create(
            owner=cls.production_user,
            shop=cls.shop,
            plan=plan,
            transaction_type=PaymentTransaction.TYPE_ACTIVATION,
            amount="300.00",
            idempotency_key="admin-dashboard-payment",
            status="paid",
        )

    def setUp(self):
        self.client = APIClient()

    def test_superuser_can_access_admin_dashboard(self):
        self.client.force_authenticate(user=self.super_admin)

        response = self.client.get("/api/dashboard/admin/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["role"], "super_admin")
        self.assertIn("metrics", data)
        self.assertIn("summaries", data)
        self.assertIn("tables", data)

    def test_client_cannot_access_admin_dashboard(self):
        self.client.force_authenticate(user=self.client_user)
        response = self.client.get("/api/dashboard/admin/")
        self.assertEqual(response.status_code, 403)

    def test_partner_cannot_access_admin_dashboard(self):
        self.client.force_authenticate(user=self.partner_user)
        response = self.client.get("/api/dashboard/admin/")
        self.assertEqual(response.status_code, 403)

    def test_production_cannot_access_admin_dashboard(self):
        self.client.force_authenticate(user=self.production_user)
        response = self.client.get("/api/dashboard/admin/")
        self.assertEqual(response.status_code, 403)

    def test_empty_dashboard_returns_zeroes_not_server_error(self):
        ManagedJob.objects.all().delete()
        JobAssignment.objects.all().delete()
        JobPayment.objects.all().delete()
        QuoteRequest.objects.all().delete()
        PaymentTransaction.objects.all().delete()

        self.client.force_authenticate(user=self.super_admin)
        response = self.client.get("/api/dashboard/admin/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summaries"]["quotes"]["total_quote_requests"], 0)
        self.assertEqual(data["summaries"]["jobs"]["total_jobs"], 0)
        self.assertEqual(data["summaries"]["payments"]["total_payments_initiated"], 0)

    def test_comparison_helper_counts_windows_correctly(self):
        now = timezone.now()
        queryset = User.objects.filter(id__in=[self.client_user.id, self.partner_user.id])
        User.objects.filter(id=self.client_user.id).update(date_joined=now - timedelta(minutes=10))
        User.objects.filter(id=self.partner_user.id).update(date_joined=now - timedelta(minutes=70))

        result = get_time_window_comparison(
            queryset,
            "date_joined",
            now - timedelta(hours=1),
            now,
            now - timedelta(hours=2),
            now - timedelta(hours=1),
        )

        self.assertEqual(result["current_value"], 1)
        self.assertEqual(result["previous_value"], 1)
        self.assertEqual(result["trend"], "flat")

    def test_percent_change_handles_previous_zero_safely(self):
        result = calculate_change(Decimal("4"), Decimal("0"))
        self.assertEqual(result["absolute_change"], 4)
        self.assertIsNone(result["percent_change"])

    def test_mpesa_summary_works_with_no_payments(self):
        JobPayment.objects.all().delete()
        PaymentTransaction.objects.all().delete()
        self.client.force_authenticate(user=self.super_admin)

        response = self.client.get("/api/dashboard/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payments_monitor"]["statuses"]["confirmed"], 0)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PartnerClientCreateTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.partner = User.objects.create_user(
            email="partner-client-create@test.com",
            password="pass12345",
            role=User.Role.PARTNER,
            partner_profile_enabled=True,
            name="Partner Creator",
        )
        self.non_partner = User.objects.create_user(
            email="non-partner-client-create@test.com",
            password="pass12345",
            role=User.Role.CLIENT,
            name="Regular Client",
        )
        self.production_user = User.objects.create_user(
            email="production-client-create@test.com",
            password="pass12345",
            role=User.Role.PRODUCTION,
            name="Production User",
        )

    def test_partner_get_with_no_clients_returns_empty_results(self):
        self.client.force_authenticate(user=self.partner)

        response = self.client.get("/api/dashboard/partner/clients/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"role": "partner", "results": []})

    def test_partner_post_creates_client_and_get_returns_it(self):
        self.client.force_authenticate(user=self.partner)

        create_response = self.client.post(
            "/api/dashboard/partner/clients/",
            {
                "name": "Jane Client",
                "phone": "+254712345678",
                "email": "jane@example.com",
                "company": "Jane Ltd",
            },
            format="json",
        )

        self.assertIn(create_response.status_code, (200, 201))
        self.assertIsNotNone(create_response.json()["client_id"])

        list_response = self.client.get("/api/dashboard/partner/clients/")

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()["results"]), 1)
        self.assertEqual(list_response.json()["results"][0]["name"], "Jane Client")
        self.assertEqual(list_response.json()["results"][0]["phone"], "+254712345678")

    def test_partner_create_by_phone_is_idempotent(self):
        self.client.force_authenticate(user=self.partner)

        first_response = self.client.post(
            "/api/dashboard/partner/clients/",
            {
                "name": "First Managed Client",
                "phone": "+254712000111",
                "email": "",
                "company": "Acme Ltd",
            },
            format="json",
        )
        self.assertEqual(first_response.status_code, 201)
        first_payload = first_response.json()
        self.assertTrue(first_payload["is_new"])

        second_response = self.client.post(
            "/api/dashboard/partner/clients/",
            {
                "name": "First Managed Client",
                "phone": "+254712000111",
                "email": "",
                "company": "Acme Ltd",
            },
            format="json",
        )
        self.assertEqual(second_response.status_code, 200)
        second_payload = second_response.json()
        self.assertEqual(second_payload["client_id"], first_payload["client_id"])
        self.assertFalse(second_payload["is_new"])
        self.assertEqual(PartnerClient.objects.filter(partner=self.partner).count(), 1)

    def test_partner_get_handles_null_client_user(self):
        PartnerClient.objects.create(
            partner=self.partner,
            client_user=None,
            name="Loose Record",
            phone="",
            email="",
            company="",
        )
        self.client.force_authenticate(user=self.partner)

        response = self.client.get("/api/dashboard/partner/clients/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["name"], "Loose Record")
        self.assertIsNone(response.json()["results"][0]["client_id"])

    def test_non_partner_cannot_create_partner_client(self):
        self.client.force_authenticate(user=self.non_partner)

        response = self.client.post(
            "/api/dashboard/partner/clients/",
            {"name": "Blocked Client", "phone": "+254799999999"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_non_partner_cannot_get_partner_clients(self):
        self.client.force_authenticate(user=self.non_partner)

        response = self.client.get("/api/dashboard/partner/clients/")

        self.assertEqual(response.status_code, 403)

    def test_production_cannot_access_partner_clients(self):
        self.client.force_authenticate(user=self.production_user)

        get_response = self.client.get("/api/dashboard/partner/clients/")
        post_response = self.client.post(
            "/api/dashboard/partner/clients/",
            {"name": "Blocked Client", "phone": "+254700000000"},
            format="json",
        )

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
