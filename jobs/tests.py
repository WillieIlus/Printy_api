"""JobShare tests: token security, permissions."""
import re
from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from artwork.models import UploadedArtwork
from jobs.admin import JobRequestAdmin
from jobs.file_services import (
    approve_job_proof,
    attach_uploaded_artwork_to_managed_job,
    create_job_file,
    get_visible_job_files_for_actor,
    import_legacy_files_to_managed_job,
    mark_file_print_ready,
    request_revision,
    upload_artwork_for_managed_job,
    upload_proof_for_managed_job,
)
from jobs.assignment_services import (
    accept_assignment,
    mark_assignment_completed,
    mark_assignment_finishing,
    mark_assignment_in_production,
    mark_assignment_ready,
    reject_assignment,
    report_assignment_issue,
)
from jobs.managed_services import (
    attach_production_order_to_assignment,
    create_assignment_for_managed_job,
    create_managed_job_from_accepted_quote,
)
from jobs.models import JobAssignment, JobClaim, JobFile, JobPayment, JobRequest, JobSettlementSplit, ManagedJob, ManagedJobEvent
from jobs.payment_services import (
    create_job_payment,
    generate_job_account_reference,
    handle_job_mpesa_callback,
    initialize_settlement_for_managed_job,
    initiate_job_stk_push,
    mark_payment_confirmed,
    mark_settlement_release_ready,
    reconcile_job_payment_status,
)
from jobs.serializers import (
    JobAssignmentSerializer,
    JobFileSerializer,
    JobPaymentSerializer,
    JobSettlementSplitSerializer,
)
from jobs.workflow import (
    canonical_workflow_definition,
    managed_status_from_production_order,
    managed_status_from_quote_request_status,
    managed_status_from_shop_quote_status,
    project_workflow_state,
)
from production.models import Customer, ProductionOrder
from quotes.choices import QuoteStatus, ShopQuoteStatus
from quotes.models import QuoteRequest, ShopQuote
from shops.models import Shop
from services.pricing.urgency import apply_priority_pricing


class JobRequestAPITestCase(TestCase):
    """Test JobShare API: create, list, permissions."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="printer@test.com", password="pass")
        self.other = User.objects.create_user(email="other@test.com", password="pass")

    def test_unauthenticated_cannot_create(self):
        """Only authenticated users can create job requests."""
        r = self.client.post(
            "/api/job-requests/",
            {"title": "Brochure 500 pcs", "specs": {"product": "A4 Brochure", "qty": 500}},
            format="json",
        )
        self.assertEqual(r.status_code, 401)

    def test_unauthenticated_cannot_list(self):
        """Only authenticated users can list job requests."""
        r = self.client.get("/api/job-requests/")
        self.assertEqual(r.status_code, 401)

    def test_authenticated_can_create(self):
        """Authenticated printer can create job request."""
        self.client.force_authenticate(user=self.user)
        r = self.client.post(
            "/api/job-requests/",
            {
                "title": "Brochure 500 pcs",
                "specs": {"product": "A4 Brochure", "qty": 500},
                "location": "Nairobi",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertEqual(data["title"], "Brochure 500 pcs")
        self.assertEqual(data["status"], "OPEN")
        self.assertEqual(data["status_label"], "Open")
        self.assertEqual(data["created_by"], self.user.id)

    def test_list_filter_by_status(self):
        """GET /api/job-requests/?status=OPEN filters correctly."""
        self.client.force_authenticate(user=self.user)
        JobRequest.objects.create(
            created_by=self.user,
            title="Open job",
            status=JobRequest.OPEN,
            public_token=None,
        )
        JobRequest.objects.create(
            created_by=self.user,
            title="Closed job",
            status=JobRequest.CLOSED,
            public_token=None,
        )
        r = self.client.get("/api/job-requests/?status=OPEN")
        self.assertEqual(r.status_code, 200)
        results = r.json().get("results", r.json())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Open job")


class JobRequestTokenSecurityTestCase(TestCase):
    """Test token is un-guessable and secure."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="printer@test.com", password="pass")

    def test_token_format(self):
        """Public token is base64url, 43 chars (32 bytes)."""
        self.client.force_authenticate(user=self.user)
        r = self.client.post(
            "/api/job-requests/",
            {"title": "Test", "specs": {}},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        job_id = r.json()["id"]
        r2 = self.client.post(f"/api/job-requests/{job_id}/whatsapp-share/", {}, format="json")
        self.assertEqual(r2.status_code, 200)
        url = r2.json()["public_view_url"]
        token = url.split("/track-job/")[-1].rstrip("/")
        # Base64url: A-Za-z0-9_- only, typically 43 chars for 32 bytes
        self.assertTrue(re.match(r"^[A-Za-z0-9_-]{40,50}$", token), f"Token format invalid: {token!r}")
        self.assertGreaterEqual(len(token), 40)

    def test_token_unguessable(self):
        """Sequential IDs must not predict token."""
        self.client.force_authenticate(user=self.user)
        tokens = []
        for i in range(3):
            r = self.client.post(
                "/api/job-requests/",
                {"title": f"Job {i}", "specs": {}},
                format="json",
            )
            self.assertEqual(r.status_code, 201)
            r2 = self.client.post(
                f"/api/job-requests/{r.json()['id']}/whatsapp-share/",
                {},
                format="json",
            )
            tokens.append(r2.json()["public_view_url"].split("/track-job/")[-1].rstrip("/"))
        # All tokens must be unique and not sequential
        self.assertEqual(len(set(tokens)), 3)
        for t in tokens:
            self.assertFalse(t.isdigit(), "Token must not be numeric ID")

    def test_public_view_returns_safe_fields_only(self):
        """Public token view must not expose internal/sensitive data."""
        self.client.force_authenticate(user=self.user)
        r = self.client.post(
            "/api/job-requests/",
            {
                "title": "Secret job",
                "specs": {"internal_cost": 100, "product": "Brochure"},
                "location": "Nairobi",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        job_id = r.json()["id"]
        r2 = self.client.post(f"/api/job-requests/{job_id}/whatsapp-share/", {}, format="json")
        token = r2.json()["public_view_url"].split("/track-job/")[-1].rstrip("/")
        # Public view — no auth
        r3 = self.client.get(f"/api/public/job/{token}/")
        self.assertEqual(r3.status_code, 200)
        data = r3.json()
        self.assertIn("title", data)
        self.assertIn("specs", data)
        self.assertIn("location", data)
        self.assertEqual(data["status_label"], "Open")
        self.assertNotIn("created_by", data)
        self.assertNotIn("created_by_email", data)
        # Specs may include keys - we don't filter specs, but we don't expose created_by
        self.assertIn("claim_cta", data)
        self.assertTrue(data["requires_login"])

    def test_invalid_token_404(self):
        """Invalid token returns 404."""
        r = self.client.get("/api/public/job/invalid-token-xyz/")
        self.assertEqual(r.status_code, 404)

    @override_settings(FRONTEND_URL="https://printy.ke")
    def test_admin_public_link_uses_track_job_route(self):
        job = JobRequest.objects.create(
            created_by=self.user,
            title="Admin link job",
            status=JobRequest.OPEN,
            public_token="test-public-token",
        )
        admin_instance = JobRequestAdmin(JobRequest, None)

        html = admin_instance.public_link(job)

        self.assertIn("/track-job/test-public-token", str(html))
        self.assertNotIn("/public/job/", str(html))
        self.assertNotIn("/job/test-public-token", str(html))


class JobClaimAPITestCase(TestCase):
    """Test claiming workflow: only open jobs, only owner can accept/reject, accepting closes job."""

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(email="owner@test.com", password="pass")
        self.claimant = User.objects.create_user(email="claimant@test.com", password="pass")

    def test_only_open_jobs_can_be_claimed(self):
        """Only OPEN jobs can be claimed."""
        job = JobRequest.objects.create(
            created_by=self.owner,
            title="Open job",
            status=JobRequest.CLOSED,
            public_token=None,
        )
        self.client.force_authenticate(user=self.claimant)
        r = self.client.post(
            f"/api/job-requests/{job.id}/claims/",
            {"message": "I can do this"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Only open jobs", r.json()["detail"])

    def test_only_job_owner_can_accept(self):
        """Only job owner can accept claims."""
        job = JobRequest.objects.create(
            created_by=self.owner,
            title="Open job",
            status=JobRequest.OPEN,
            public_token=None,
        )
        claim = JobClaim.objects.create(
            job_request=job,
            claimed_by=self.claimant,
            status=JobClaim.PENDING,
        )
        self.client.force_authenticate(user=self.claimant)
        r = self.client.post(f"/api/job-claims/{claim.id}/accept/", {}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Only the job owner", r.json()["detail"])

    def test_only_job_owner_can_reject(self):
        """Only job owner can reject claims."""
        job = JobRequest.objects.create(
            created_by=self.owner,
            title="Open job",
            status=JobRequest.OPEN,
            public_token=None,
        )
        claim = JobClaim.objects.create(
            job_request=job,
            claimed_by=self.claimant,
            status=JobClaim.PENDING,
        )
        self.client.force_authenticate(user=self.claimant)
        r = self.client.post(f"/api/job-claims/{claim.id}/reject/", {}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_accepting_closes_job(self):
        """Accepting a claim marks job as CLAIMED and creates notification."""
        job = JobRequest.objects.create(
            created_by=self.owner,
            title="Open job",
            status=JobRequest.OPEN,
            public_token=None,
        )
        claim = JobClaim.objects.create(
            job_request=job,
            claimed_by=self.claimant,
            status=JobClaim.PENDING,
        )
        self.client.force_authenticate(user=self.owner)
        r = self.client.post(f"/api/job-claims/{claim.id}/accept/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        claim.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(claim.status, JobClaim.ACCEPTED)
        self.assertEqual(job.status, JobRequest.CLAIMED)
        from jobs.models import JobNotification
        self.assertTrue(JobNotification.objects.filter(job_claim=claim, user=self.claimant).exists())


class ManagedJobFoundationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="managed@test.com", password="pass12345", role="client")
        self.owner = User.objects.create_user(email="shop@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Managed Shop", slug="managed-shop", is_active=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Managed Customer", email="managed@test.com")
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.user,
            customer=self.customer,
            customer_name="Managed Customer",
            customer_email="managed@test.com",
            status=QuoteStatus.QUOTED,
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("3500.00"),
        )

    def test_managed_job_defaults_are_workflow_safe(self):
        managed_job = ManagedJob.objects.create(
            title="Managed business cards",
            source_quote_request=self.quote_request,
            source_shop_quote=self.shop_quote,
            client=self.user,
            customer=self.customer,
            assigned_shop=self.shop,
            created_by=self.user,
        )
        self.assertTrue(managed_job.managed_reference.startswith("MJ-"))
        self.assertEqual(managed_job.status, "draft")
        self.assertEqual(managed_job.payment_status, "pending")
        self.assertEqual(managed_job.assignment_status, "unassigned")
        self.assertEqual(managed_job.exception_status, "clear")

    def test_quote_and_production_states_normalize_into_canonical_workflow(self):
        self.assertEqual(managed_status_from_quote_request_status(QuoteStatus.QUOTED), "quoted")
        self.assertEqual(managed_status_from_quote_request_status(QuoteStatus.CLOSED), "awaiting_payment")
        self.assertEqual(managed_status_from_shop_quote_status(ShopQuoteStatus.ACCEPTED), "awaiting_payment")
        self.assertEqual(
            managed_status_from_production_order(status=ProductionOrder.IN_PROGRESS, delivery_status=ProductionOrder.DELIVERY_PENDING),
            "in_production",
        )
        self.assertEqual(
            managed_status_from_production_order(status=ProductionOrder.COMPLETED, delivery_status=ProductionOrder.DELIVERY_DELIVERED),
            "delivered",
        )

    def test_workflow_projection_separates_actor_visibility_from_authority(self):
        projection = project_workflow_state(
            status="payment_confirmed",
            actor="client",
            payment_status="confirmed",
            assignment_status="assigned",
            exception_status="clear",
        )
        self.assertEqual(projection["label"], "Payment confirmed")
        self.assertIsNone(projection["payment_status"])
        self.assertIsNone(projection["assignment_status"])
        self.assertEqual(projection["allowed_transition_actors"], [])

        ops_projection = project_workflow_state(
            status="payment_confirmed",
            actor="ops",
            payment_status="confirmed",
            assignment_status="assigned",
            exception_status="clear",
        )
        self.assertEqual(ops_projection["payment_status"], "confirmed")
        self.assertIn("ops", ops_projection["allowed_transition_actors"])

    def test_canonical_workflow_definition_contains_transition_governance(self):
        definition = canonical_workflow_definition()
        self.assertIn("sequence", definition)
        self.assertIn("transition_owners", definition)
        self.assertIn("awaiting_payment", definition["sequence"])


class JobAssignmentFoundationTestCase(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(email="assignment-client@test.com", password="pass12345", role="client")
        self.owner = User.objects.create_user(email="assignment-owner@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Assignment Shop", slug="assignment-shop", is_active=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Assignment Customer", email="assignment-client@test.com")
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Assignment Customer",
            customer_email="assignment-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={
                "source": "calculator_draft_send",
                "visibility": {
                    "actor": "client",
                    "topology_mode": "managed",
                    "exposes_internal_economics": False,
                },
            },
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("4200.00"),
        )
        self.managed_job = ManagedJob.objects.create(
            title="Assigned managed job",
            source_quote_request=self.quote_request,
            source_shop_quote=self.shop_quote,
            client=self.client_user,
            customer=self.customer,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="awaiting_payment",
        )

    def test_assignment_serializer_masks_shop_name_for_client_and_shows_for_shop(self):
        assignment = JobAssignment.objects.create(
            managed_job=self.managed_job,
            assigned_shop=self.shop,
            source_shop_quote=self.shop_quote,
            status="pending",
        )
        factory = APIClient()
        request = type("Request", (), {"user": self.client_user})()
        client_payload = JobAssignmentSerializer(assignment, context={"request": request}).data
        self.assertEqual(client_payload["shop_name"], "Verified Print Partner")

        shop_request = type("Request", (), {"user": self.owner})()
        shop_payload = JobAssignmentSerializer(assignment, context={"request": shop_request}).data
        self.assertEqual(shop_payload["shop_name"], self.shop.name)

    def test_assignment_service_is_idempotent_and_tracks_production_order(self):
        first_assignment = create_assignment_for_managed_job(
            managed_job=self.managed_job,
            shop_quote=self.shop_quote,
        )
        second_assignment = create_assignment_for_managed_job(
            managed_job=self.managed_job,
            shop_quote=self.shop_quote,
        )

        self.assertEqual(first_assignment.id, second_assignment.id)
        self.assertEqual(JobAssignment.objects.filter(managed_job=self.managed_job).count(), 1)
        self.assertEqual(first_assignment.assigned_shop_id, self.shop.id)
        self.assertEqual(first_assignment.source_shop_quote_id, self.shop_quote.id)
        self.assertEqual(first_assignment.operational_snapshot["managed_reference"], self.managed_job.managed_reference)

        production_order = ProductionOrder.objects.create(
            shop=self.shop,
            customer=self.customer,
            title="Assignment production order",
            quantity=100,
            status="in_progress",
            delivery_status="pending",
        )
        attach_production_order_to_assignment(
            assignment=first_assignment,
            production_order=production_order,
        )

        first_assignment.refresh_from_db()
        self.assertEqual(first_assignment.production_order_id, production_order.id)
        self.assertEqual(first_assignment.status, "in_production")
        self.assertEqual(first_assignment.operational_snapshot["production_order_id"], production_order.id)


class JobFileFoundationTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="job-file-client@test.com", password="pass12345", role="client")
        self.owner = User.objects.create_user(email="job-file-owner@test.com", password="pass12345", role="shop_owner")
        self.partner = User.objects.create_user(email="job-file-partner@test.com", password="pass12345", role="broker")
        self.ops = User.objects.create_user(email="job-file-ops@test.com", password="pass12345", is_staff=True)
        self.shop = Shop.objects.create(owner=self.owner, name="Job File Shop", slug="job-file-shop", is_active=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Job File Customer", email="job-file-client@test.com")
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Job File Customer",
            customer_email="job-file-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={
                "source": "calculator_draft_send",
                "visibility": {
                    "actor": "client",
                    "topology_mode": "managed",
                    "exposes_internal_economics": False,
                },
            },
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("4200.00"),
        )
        self.managed_job = ManagedJob.objects.create(
            title="Job file managed job",
            source_quote_request=self.quote_request,
            source_shop_quote=self.shop_quote,
            client=self.client_user,
            customer=self.customer,
            broker=self.partner,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="awaiting_payment",
        )
        self.assignment = JobAssignment.objects.create(
            managed_job=self.managed_job,
            assigned_shop=self.shop,
            source_shop_quote=self.shop_quote,
            status="pending",
        )

    def test_attach_uploaded_artwork_to_managed_job_is_idempotent(self):
        uploaded_artwork = UploadedArtwork.objects.create(
            file=SimpleUploadedFile("customer-art.pdf", b"%PDF-1.7 test bytes", content_type="application/pdf"),
            file_type="pdf",
        )
        first = attach_uploaded_artwork_to_managed_job(
            managed_job=self.managed_job,
            uploaded_artwork=uploaded_artwork,
            uploaded_by=self.client_user,
        )
        second = attach_uploaded_artwork_to_managed_job(
            managed_job=self.managed_job,
            uploaded_artwork=uploaded_artwork,
            uploaded_by=self.client_user,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(JobFile.objects.filter(managed_job=self.managed_job).count(), 1)
        self.assertTrue(first.original_filename.startswith("customer-art"))
        self.assertTrue(first.original_filename.endswith(".pdf"))

    def test_import_legacy_files_to_managed_job_is_idempotent(self):
        self.quote_request.attachments.create(
            file=SimpleUploadedFile("brief.pdf", b"brief bytes", content_type="application/pdf"),
            name="Customer brief",
        )
        self.shop_quote.attachments.create(
            file=SimpleUploadedFile("proof.pdf", b"proof bytes", content_type="application/pdf"),
            name="Press proof",
        )

        import_legacy_files_to_managed_job(
            managed_job=self.managed_job,
            quote_request=self.quote_request,
            shop_quote=self.shop_quote,
        )
        import_legacy_files_to_managed_job(
            managed_job=self.managed_job,
            quote_request=self.quote_request,
            shop_quote=self.shop_quote,
        )

        self.assertEqual(JobFile.objects.filter(managed_job=self.managed_job).count(), 2)
        self.assertEqual(
            set(JobFile.objects.filter(managed_job=self.managed_job).values_list("original_filename", flat=True)),
            {"Customer brief", "Press proof"},
        )

    def test_visibility_service_and_api_hide_internal_files(self):
        customer_file = create_job_file(
            managed_job=self.managed_job,
            assignment=self.assignment,
            uploaded_by=self.client_user,
            file=SimpleUploadedFile("customer.pdf", b"customer bytes", content_type="application/pdf"),
            original_filename="customer.pdf",
            file_type="customer_upload",
            visibility="client",
            notes="Customer-supplied artwork",
        )
        create_job_file(
            managed_job=self.managed_job,
            assignment=self.assignment,
            uploaded_by=self.owner,
            file=SimpleUploadedFile("press-ready.pdf", b"print bytes", content_type="application/pdf"),
            original_filename="press-ready.pdf",
            file_type="print_ready",
            visibility="shop",
            notes="Press-ready production file",
        )
        create_job_file(
            managed_job=self.managed_job,
            assignment=self.assignment,
            uploaded_by=self.ops,
            file=SimpleUploadedFile("ops-note.pdf", b"ops bytes", content_type="application/pdf"),
            original_filename="ops-note.pdf",
            file_type="proof",
            visibility="internal",
            notes="Internal quality note",
        )

        client_files = list(get_visible_job_files_for_actor(managed_job=self.managed_job, actor="client"))
        shop_files = list(get_visible_job_files_for_actor(managed_job=self.managed_job, actor="shop"))
        ops_files = list(get_visible_job_files_for_actor(managed_job=self.managed_job, actor="ops"))

        self.assertEqual([item.original_filename for item in client_files], ["customer.pdf"])
        self.assertEqual([item.original_filename for item in shop_files], ["customer.pdf", "press-ready.pdf"])
        self.assertEqual(len(ops_files), 3)

        request = type("Request", (), {"user": self.client_user, "build_absolute_uri": lambda self, path: f"http://testserver{path}"})()
        payload = JobFileSerializer(customer_file, context={"request": request}).data
        self.assertNotIn("file", payload)
        self.assertEqual(payload["notes"], "")
        self.assertIn("/api/job-files/", payload["download_url"])

        self.client.force_authenticate(user=self.client_user)
        client_response = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/files/")
        self.assertEqual(client_response.status_code, 200)
        self.assertEqual([item["original_filename"] for item in client_response.json()], ["customer.pdf"])
        self.assertFalse(any("uploaded_by" in item for item in client_response.json()))

        self.client.force_authenticate(user=self.owner)
        shop_response = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/files/")
        self.assertEqual(shop_response.status_code, 200)
        self.assertEqual(
            [item["original_filename"] for item in shop_response.json()],
            ["customer.pdf", "press-ready.pdf"],
        )

        download_response = self.client.get(f"/api/job-files/{customer_file.id}/download/")
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response["Content-Disposition"], 'attachment; filename="customer.pdf"')


class JobPaymentSettlementFoundationTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="settlement-client@test.com", password="pass12345", role="client")
        self.partner = User.objects.create_user(email="settlement-partner@test.com", password="pass12345", role="broker")
        self.owner = User.objects.create_user(email="settlement-owner@test.com", password="pass12345", role="shop_owner")
        self.ops = User.objects.create_user(email="settlement-ops@test.com", password="pass12345", is_staff=True)
        self.shop = Shop.objects.create(owner=self.owner, name="Settlement Shop", slug="settlement-shop", is_active=True)
        self.customer = Customer.objects.create(
            shop=self.shop,
            name="Settlement Customer",
            email="settlement-client@test.com",
            relationship_owner_type=Customer.RelationshipOwnerType.USER,
            relationship_owner_user=self.partner,
        )
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Settlement Customer",
            customer_email="settlement-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={
                "source": "calculator_draft_send",
                "visibility": {
                    "actor": "client",
                    "topology_mode": "managed",
                    "exposes_internal_economics": False,
                },
            },
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("1600.00"),
        )
        self.managed_job = ManagedJob.objects.create(
            title="Settlement job",
            source_quote_request=self.quote_request,
            source_shop_quote=self.shop_quote,
            client=self.client_user,
            customer=self.customer,
            broker=self.partner,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="awaiting_payment",
            payment_status="pending",
            client_total=Decimal("1600.00"),
            production_total=Decimal("1000.00"),
            platform_fee=Decimal("300.00"),
            broker_commission=Decimal("300.00"),
            relationship_snapshot={
                "owner_type": "user",
                "owner_reference": f"user:{self.partner.id}",
                "owner_user_id": self.partner.id,
                "owner_shop_id": None,
                "acquisition_source": "partner",
            },
        )

    def test_initialize_settlement_records_relationship_owner_split(self):
        settlement = initialize_settlement_for_managed_job(managed_job=self.managed_job)
        self.assertEqual(settlement.production_amount, Decimal("1000.00"))
        self.assertEqual(settlement.platform_fee, Decimal("300.00"))
        self.assertEqual(settlement.partner_commission, Decimal("300.00"))
        self.assertEqual(settlement.client_total, Decimal("1600.00"))
        self.assertEqual(settlement.commission_recipient_type, "user")
        self.assertEqual(settlement.relationship_owner_user_id, self.partner.id)

    def test_cash_payment_is_recorded_and_confirmation_updates_managed_job(self):
        payment = create_job_payment(
            managed_job=self.managed_job,
            payer=self.client_user,
            payment_method="cash",
        )
        self.assertEqual(payment.payment_status, "pending")

        mark_payment_confirmed(job_payment=payment, raw_gateway_payload={"confirmed_by": "ops"})
        payment.refresh_from_db()
        self.managed_job.refresh_from_db()
        self.assertEqual(payment.payment_status, "paid")
        self.assertIsNotNone(payment.confirmed_at)
        self.assertEqual(self.managed_job.payment_status, "confirmed")
        self.assertEqual(self.managed_job.status, "payment_confirmed")

    def test_release_ready_updates_settlement_and_managed_job(self):
        settlement = initialize_settlement_for_managed_job(managed_job=self.managed_job, payment_method="mpesa")
        mark_settlement_release_ready(settlement=settlement)
        settlement.refresh_from_db()
        self.managed_job.refresh_from_db()
        self.assertEqual(settlement.status, "release_ready")
        self.assertIsNotNone(settlement.release_ready_at)
        self.assertEqual(self.managed_job.payment_status, "release_ready")

    def test_visibility_safe_payment_and_settlement_serializers(self):
        payment = JobPayment.objects.create(
            managed_job=self.managed_job,
            payer=self.client_user,
            amount=Decimal("1600.00"),
            payment_method="mpesa",
            payment_status="paid",
            external_reference="PAY-123",
        )
        settlement = initialize_settlement_for_managed_job(managed_job=self.managed_job)

        client_request = type("Request", (), {"user": self.client_user})()
        shop_request = type("Request", (), {"user": self.owner})()
        partner_request = type("Request", (), {"user": self.partner})()
        ops_request = type("Request", (), {"user": self.ops})()

        client_payment = JobPaymentSerializer(payment, context={"request": client_request}).data
        self.assertEqual(client_payment["amount"], "1600.00")
        self.assertEqual(client_payment["external_reference"], "")

        shop_payment = JobPaymentSerializer(payment, context={"request": shop_request}).data
        self.assertIsNone(shop_payment["amount"])
        self.assertIsNone(shop_payment["expected_amount"])
        self.assertIsNone(shop_payment["received_amount"])
        self.assertEqual(shop_payment["payment_status"], "paid")
        self.assertEqual(shop_payment["status_code"], "paid")

        client_settlement = JobSettlementSplitSerializer(settlement, context={"request": client_request}).data
        self.assertEqual(client_settlement["client_total"], "1600.00")
        self.assertIsNone(client_settlement["production_amount"])
        self.assertIsNone(client_settlement["partner_commission"])

        shop_settlement = JobSettlementSplitSerializer(settlement, context={"request": shop_request}).data
        self.assertEqual(shop_settlement["production_amount"], "1000.00")
        self.assertIsNone(shop_settlement["client_total"])
        self.assertIsNone(shop_settlement["partner_commission"])

        partner_settlement = JobSettlementSplitSerializer(settlement, context={"request": partner_request}).data
        self.assertEqual(partner_settlement["partner_commission"], "300.00")
        self.assertIsNone(partner_settlement["platform_fee"])

        ops_settlement = JobSettlementSplitSerializer(settlement, context={"request": ops_request}).data
        self.assertEqual(ops_settlement["platform_fee"], "300.00")
        self.assertEqual(ops_settlement["relationship_owner_type"], "user")

    def test_shop_owned_relationship_routes_commission_to_shop(self):
        self.managed_job.relationship_snapshot = {
            "owner_type": "shop",
            "owner_reference": f"shop:{self.shop.id}",
            "owner_user_id": None,
            "owner_shop_id": self.shop.id,
            "acquisition_source": "shop",
        }
        self.managed_job.save(update_fields=["relationship_snapshot", "updated_at"])
        settlement = initialize_settlement_for_managed_job(managed_job=self.managed_job)
        self.assertEqual(settlement.commission_recipient_type, "shop")
        self.assertEqual(settlement.relationship_owner_shop_id, self.shop.id)

    def test_payment_and_settlement_endpoints_enforce_visibility_by_actor(self):
        payment = create_job_payment(
            managed_job=self.managed_job,
            payer=self.client_user,
            amount=Decimal("1600.00"),
            payment_method="mpesa",
            external_reference="PAY-HTTP-123",
        )
        mark_payment_confirmed(job_payment=payment, raw_gateway_payload={"checkout": "CHK-1"})
        settlement = initialize_settlement_for_managed_job(managed_job=self.managed_job)

        self.client.force_authenticate(user=self.client_user)
        client_payments = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/payments/")
        self.assertEqual(client_payments.status_code, 200)
        self.assertEqual(client_payments.json()[0]["amount"], "1600.00")
        self.assertEqual(client_payments.json()[0]["external_reference"], "")

        client_settlement = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/settlement/")
        self.assertEqual(client_settlement.status_code, 200)
        self.assertEqual(client_settlement.json()["client_total"], "1600.00")
        self.assertIsNone(client_settlement.json()["production_amount"])
        self.assertIsNone(client_settlement.json()["partner_commission"])

        self.client.force_authenticate(user=self.owner)
        shop_payments = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/payments/")
        self.assertEqual(shop_payments.status_code, 200)
        self.assertIsNone(shop_payments.json()[0]["amount"])
        self.assertIsNone(shop_payments.json()[0]["expected_amount"])
        self.assertIsNone(shop_payments.json()[0]["received_amount"])
        self.assertEqual(shop_payments.json()[0]["payment_status"], "paid")

        shop_settlement = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/settlement/")
        self.assertEqual(shop_settlement.status_code, 200)
        self.assertEqual(shop_settlement.json()["production_amount"], "1000.00")
        self.assertIsNone(shop_settlement.json()["client_total"])
        self.assertIsNone(shop_settlement.json()["partner_commission"])

        self.client.force_authenticate(user=self.partner)
        partner_settlement = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/settlement/")
        self.assertEqual(partner_settlement.status_code, 200)
        self.assertEqual(partner_settlement.json()["partner_commission"], "300.00")
        self.assertEqual(partner_settlement.json()["commission_recipient_type"], "user")
        self.assertIsNone(partner_settlement.json()["platform_fee"])

        self.client.force_authenticate(user=self.ops)
        ops_payments = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/payments/")
        self.assertEqual(ops_payments.status_code, 200)
        self.assertEqual(ops_payments.json()[0]["external_reference"], "PAY-HTTP-123")

        ops_settlement = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/settlement/")
        self.assertEqual(ops_settlement.status_code, 200)
        self.assertEqual(ops_settlement.json()["platform_fee"], "300.00")
        self.assertEqual(ops_settlement.json()["relationship_owner_type"], "user")
        self.assertEqual(ops_settlement.json()["relationship_owner_reference"], f"user:{self.partner.id}")
        self.assertEqual(ops_settlement.json()["id"], settlement.id)


@override_settings(
    MPESA_BASE_URL="https://sandbox.safaricom.co.ke",
    MPESA_CALLBACK_URL="https://api.printy.ke/api/payments/mpesa/callback/",
    MPESA_CONSUMER_KEY="test-key",
    MPESA_CONSUMER_SECRET="test-secret",
    MPESA_SHORTCODE="174379",
    MPESA_PASSKEY="test-passkey",
    MPESA_ENV="sandbox",
)
class ManagedJobMpesaHardeningTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="mpesa-client@test.com", password="pass12345", role="client")
        self.ops = User.objects.create_user(email="mpesa-ops@test.com", password="pass12345", is_staff=True)
        self.owner = User.objects.create_user(email="mpesa-owner@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Mpesa Shop", slug="mpesa-shop", is_active=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Mpesa Customer", email="mpesa-client@test.com")
        self.managed_job = ManagedJob.objects.create(
            title="Mpesa managed job",
            client=self.client_user,
            customer=self.customer,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="awaiting_payment",
            payment_status="pending",
            client_total=Decimal("2500.00"),
            production_total=Decimal("1500.00"),
            platform_fee=Decimal("500.00"),
            broker_commission=Decimal("500.00"),
        )

    def _stk_success_response(self):
        response = Mock()
        response.json.return_value = {
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing",
            "CustomerMessage": "Success",
            "MerchantRequestID": "29115-34620561-1",
            "CheckoutRequestID": "ws_CO_123456789",
        }
        response.raise_for_status.return_value = None
        return response

    def _stk_callback_payload(self, amount="2500.00", receipt="TIH8QNX7PY", checkout_id="ws_CO_123456789"):
        return {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "29115-34620561-1",
                    "CheckoutRequestID": checkout_id,
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": float(amount)},
                            {"Name": "MpesaReceiptNumber", "Value": receipt},
                            {"Name": "TransactionDate", "Value": 20260513120000},
                            {"Name": "PhoneNumber", "Value": 254712345678},
                        ]
                    },
                }
            }
        }

    def _c2b_payload(self, amount="2500.00", receipt="QWE12345", account_reference=None):
        return {
            "TransID": receipt,
            "TransAmount": amount,
            "MSISDN": "254712345678",
            "BillRefNumber": account_reference or generate_job_account_reference(managed_job=self.managed_job),
            "TransTime": "20260513120500",
        }

    @patch("jobs.payment_services.requests.post")
    @patch("jobs.payment_services.get_mpesa_token", return_value="test-token")
    def test_stk_initiation_creates_account_reference_and_checkout_identifiers(self, _token, mock_post):
        mock_post.return_value = self._stk_success_response()

        payment = initiate_job_stk_push(
            managed_job=self.managed_job,
            payer=self.client_user,
            phone_number="0712345678",
        )

        self.assertEqual(payment.account_reference, f"MJ-{self.managed_job.id}")
        self.assertEqual(payment.checkout_request_id, "ws_CO_123456789")
        self.assertEqual(payment.merchant_request_id, "29115-34620561-1")
        self.assertEqual(payment.payment_status, "pending")
        self.assertEqual(payment.reconciliation_status, "pending")
        self.managed_job.refresh_from_db()
        self.assertEqual(self.managed_job.payment_status, "confirmation_pending")

    @patch("jobs.payment_services.requests.post")
    @patch("jobs.payment_services.get_mpesa_token", return_value="test-token")
    def test_stk_endpoint_reuses_existing_pending_attempt_for_repeat_request(self, _token, mock_post):
        mock_post.return_value = self._stk_success_response()
        self.client.force_authenticate(user=self.client_user)

        first = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/payments/mpesa/stk-push/",
            {"phone_number": "0712345678", "amount": "2500.00"},
            format="json",
        )
        second = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/payments/mpesa/stk-push/",
            {"phone_number": "0712345678", "amount": "2500.00"},
            format="json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(JobPayment.objects.count(), 1)
        self.assertEqual(first.json()["checkout_request_id"], "")
        self.assertEqual(second.json()["id"], first.json()["id"])

    @patch("jobs.payment_services.requests.post")
    @patch("jobs.payment_services.get_mpesa_token", return_value="test-token")
    def test_stk_endpoint_ignores_lower_client_amount_and_uses_managed_job_client_total(self, _token, mock_post):
        mock_post.return_value = self._stk_success_response()
        self.client.force_authenticate(user=self.client_user)

        response = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/payments/mpesa/stk-push/",
            {"phone_number": "0712345678", "amount": "1200.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        payment = JobPayment.objects.get(managed_job=self.managed_job)
        self.assertEqual(payment.expected_amount, Decimal("2500.00"))
        self.assertEqual(payment.amount, Decimal("2500.00"))

    @patch("jobs.payment_services.requests.post")
    @patch("jobs.payment_services.get_mpesa_token", return_value="test-token")
    def test_stk_endpoint_ignores_higher_client_amount_and_uses_managed_job_client_total(self, _token, mock_post):
        mock_post.return_value = self._stk_success_response()
        self.client.force_authenticate(user=self.client_user)

        response = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/payments/mpesa/stk-push/",
            {"phone_number": "0712345678", "amount": "9999.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        payment = JobPayment.objects.get(managed_job=self.managed_job)
        self.assertEqual(payment.expected_amount, Decimal("2500.00"))
        self.assertEqual(payment.amount, Decimal("2500.00"))

    def test_stk_endpoint_rejects_missing_or_zero_client_total(self):
        self.client.force_authenticate(user=self.client_user)
        self.managed_job.client_total = Decimal("0.00")
        self.managed_job.save(update_fields=["client_total", "updated_at"])

        response = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/payments/mpesa/stk-push/",
            {"phone_number": "0712345678"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Job pricing is not finalised. Cannot initiate payment.")

    def test_stk_callback_confirms_payment_and_updates_managed_job(self):
        payment = create_job_payment(
            managed_job=self.managed_job,
            payer=self.client_user,
            amount=Decimal("2500.00"),
            expected_amount=Decimal("2500.00"),
            payment_method="mpesa",
            payment_channel="stk_push",
            account_reference=f"MJ-{self.managed_job.id}",
            payer_phone="254712345678",
            external_reference=f"MJ-{self.managed_job.id}",
        )
        payment.checkout_request_id = "ws_CO_123456789"
        payment.merchant_request_id = "29115-34620561-1"
        payment.payment_status = "pending"
        payment.save(update_fields=["checkout_request_id", "merchant_request_id", "payment_status", "updated_at"])

        response = self.client.post("/api/payments/mpesa/callback/", self._stk_callback_payload(), format="json")
        payment.refresh_from_db()
        self.managed_job.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.payment_status, "paid")
        self.assertEqual(payment.reconciliation_status, "confirmed")
        self.assertEqual(payment.mpesa_receipt_number, "TIH8QNX7PY")
        self.assertEqual(str(payment.received_amount), "2500.00")
        self.assertEqual(self.managed_job.payment_status, "confirmed")
        self.assertEqual(self.managed_job.status, "payment_confirmed")

    def test_duplicate_stk_callback_is_idempotent(self):
        payment = create_job_payment(
            managed_job=self.managed_job,
            payer=self.client_user,
            amount=Decimal("2500.00"),
            expected_amount=Decimal("2500.00"),
            payment_method="mpesa",
            payment_channel="stk_push",
            account_reference=f"MJ-{self.managed_job.id}",
        )
        payment.checkout_request_id = "ws_CO_123456789"
        payment.payment_status = "pending"
        payment.save(update_fields=["checkout_request_id", "payment_status", "updated_at"])

        payload = self._stk_callback_payload()
        first = self.client.post("/api/payments/mpesa/callback/", payload, format="json")
        second = self.client.post("/api/payments/mpesa/callback/", payload, format="json")
        payment.refresh_from_db()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(payment.payment_status, "paid")
        self.assertEqual(payment.mpesa_receipt_number, "TIH8QNX7PY")

    def test_manual_paybill_confirmation_matches_by_account_reference(self):
        payment = create_job_payment(
            managed_job=self.managed_job,
            payer=self.client_user,
            amount=Decimal("2500.00"),
            expected_amount=Decimal("2500.00"),
            payment_method="mpesa",
            payment_channel="paybill_manual",
            account_reference=f"MJ-{self.managed_job.id}",
        )

        response = self.client.post(
            "/api/payments/mpesa/callback/",
            self._c2b_payload(account_reference=payment.account_reference),
            format="json",
        )
        payment.refresh_from_db()
        self.managed_job.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.payment_status, "paid")
        self.assertEqual(payment.payment_channel, "paybill_manual")
        self.assertEqual(self.managed_job.payment_status, "confirmed")

    def test_wrong_amount_moves_payment_to_amount_mismatch(self):
        payment = create_job_payment(
            managed_job=self.managed_job,
            payer=self.client_user,
            amount=Decimal("2500.00"),
            expected_amount=Decimal("2500.00"),
            payment_method="mpesa",
            payment_channel="paybill_manual",
            account_reference=f"MJ-{self.managed_job.id}",
        )

        response = self.client.post(
            "/api/payments/mpesa/callback/",
            self._c2b_payload(amount="2000.00", account_reference=payment.account_reference, receipt="QWE12346"),
            format="json",
        )
        payment.refresh_from_db()
        self.managed_job.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.payment_status, "needs_review")
        self.assertEqual(payment.reconciliation_status, "amount_mismatch")
        self.assertEqual(self.managed_job.payment_status, "pending")

    def test_unknown_account_reference_is_acknowledged_without_matching_payment(self):
        response = self.client.post(
            "/api/payments/mpesa/callback/",
            self._c2b_payload(account_reference="MJ-UNKNOWN"),
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(JobPayment.objects.count(), 0)

    @patch("jobs.payment_services.query_transaction_status")
    def test_transaction_status_query_readiness_updates_confirmed_payment(self, mock_query):
        mock_query.return_value = {"ResponseCode": "0", "ResultCode": "0", "ResultDesc": "Success"}
        payment = create_job_payment(
            managed_job=self.managed_job,
            payer=self.client_user,
            amount=Decimal("2500.00"),
            expected_amount=Decimal("2500.00"),
            payment_method="mpesa",
            payment_channel="stk_push",
            account_reference=f"MJ-{self.managed_job.id}",
        )
        payment.checkout_request_id = "ws_CO_123456789"
        payment.payment_status = "pending"
        payment.save(update_fields=["checkout_request_id", "payment_status", "updated_at"])

        reconcile_job_payment_status(job_payment=payment)
        payment.refresh_from_db()
        self.managed_job.refresh_from_db()

        self.assertEqual(payment.payment_status, "paid")
        self.assertEqual(payment.reconciliation_status, "confirmed")
        self.assertEqual(self.managed_job.payment_status, "confirmed")


class JobProofLifecycleTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="proof-client@test.com", password="pass12345", role="client")
        self.partner = User.objects.create_user(email="proof-partner@test.com", password="pass12345", role="broker")
        self.owner = User.objects.create_user(email="proof-owner@test.com", password="pass12345", role="shop_owner")
        self.ops = User.objects.create_user(email="proof-ops@test.com", password="pass12345", is_staff=True)
        self.shop = Shop.objects.create(owner=self.owner, name="Proof Shop", slug="proof-shop", is_active=True)
        self.customer = Customer.objects.create(
            shop=self.shop,
            name="Proof Customer",
            email="proof-client@test.com",
            relationship_owner_type=Customer.RelationshipOwnerType.USER,
            relationship_owner_user=self.partner,
        )
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Proof Customer",
            customer_email="proof-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={"visibility": {"topology_mode": "managed", "actor": "client"}},
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("2500.00"),
        )
        self.managed_job = ManagedJob.objects.create(
            title="Proof job",
            source_quote_request=self.quote_request,
            source_shop_quote=self.shop_quote,
            client=self.client_user,
            customer=self.customer,
            broker=self.partner,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="assigned",
            payment_status="confirmed",
            relationship_snapshot={"owner_type": "user", "owner_user_id": self.partner.id},
        )
        self.assignment = JobAssignment.objects.create(
            managed_job=self.managed_job,
            assigned_shop=self.shop,
            source_shop_quote=self.shop_quote,
            status="accepted",
        )

    def test_proof_lifecycle_helpers_update_statuses(self):
        proof = upload_proof_for_managed_job(
            managed_job=self.managed_job,
            assignment=self.assignment,
            uploaded_by=self.owner,
            file=SimpleUploadedFile("proof.pdf", b"proof bytes", content_type="application/pdf"),
            original_filename="proof.pdf",
        )
        self.assertEqual(proof.status, "proof_uploaded")

        request_revision(job_file=proof, actor=self.client_user, notes="Please fix the spacing.")
        proof.refresh_from_db()
        self.assertEqual(proof.status, "revision_requested")

        approve_job_proof(job_file=proof, actor=self.client_user)
        proof.refresh_from_db()
        self.assertEqual(proof.status, "proof_approved")

        mark_file_print_ready(job_file=proof, actor=self.owner)
        proof.refresh_from_db()
        self.assertEqual(proof.status, "print_ready")
        self.assertEqual(proof.file_type, "print_ready")
        self.assertEqual(proof.visibility, "shop")

    def test_proof_endpoints_enforce_visibility_and_actions(self):
        self.client.force_authenticate(user=self.owner)
        upload_response = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/files/proofs/",
            {"file": SimpleUploadedFile("proof.pdf", b"proof bytes", content_type="application/pdf"), "note": "Client proof"},
        )
        self.assertEqual(upload_response.status_code, 201)
        job_file_id = upload_response.json()["id"]

        self.client.force_authenticate(user=self.client_user)
        approve_response = self.client.post(f"/api/job-files/{job_file_id}/approve/", {"note": "Looks good"}, format="json")
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.json()["status"], "proof_approved")

        self.client.force_authenticate(user=self.partner)
        revision_response = self.client.post(f"/api/job-files/{job_file_id}/request-revision/", {"note": "Need a logo update"}, format="json")
        self.assertEqual(revision_response.status_code, 200)
        self.assertEqual(revision_response.json()["status"], "revision_requested")

        self.client.force_authenticate(user=self.owner)
        ready_response = self.client.post(f"/api/job-files/{job_file_id}/mark-print-ready/", {"note": "Ready for press"}, format="json")
        self.assertEqual(ready_response.status_code, 200)
        self.assertEqual(ready_response.json()["status"], "print_ready")


class JobAssignmentActionsTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="assign-client@test.com", password="pass12345", role="client")
        self.owner = User.objects.create_user(email="assign-owner@test.com", password="pass12345", role="shop_owner")
        self.ops = User.objects.create_user(email="assign-ops@test.com", password="pass12345", is_staff=True)
        self.shop = Shop.objects.create(owner=self.owner, name="Assign Shop", slug="assign-shop", is_active=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Assign Customer", email="assign-client@test.com")
        self.managed_job = ManagedJob.objects.create(
            title="Assignment action job",
            client=self.client_user,
            customer=self.customer,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="payment_confirmed",
            payment_status="confirmed",
            assignment_status="assignment_pending",
        )
        self.assignment = JobAssignment.objects.create(
            managed_job=self.managed_job,
            assigned_shop=self.shop,
            status="pending",
        )
        self.production_order = ProductionOrder.objects.create(
            shop=self.shop,
            customer=self.customer,
            title="Production adapter order",
            quantity=250,
            status="pending",
            delivery_status="pending",
        )
        self.assignment.production_order = self.production_order
        self.assignment.save(update_fields=["production_order", "updated_at"])

    def test_assignment_service_transitions_sync_managed_job_and_adapter(self):
        accept_assignment(assignment=self.assignment, actor=self.owner)
        self.assignment.refresh_from_db()
        self.managed_job.refresh_from_db()
        self.production_order.refresh_from_db()
        self.assertEqual(self.assignment.status, "accepted")
        self.assertEqual(self.managed_job.status, "assigned")
        self.assertEqual(self.production_order.status, "pending")

        mark_assignment_in_production(assignment=self.assignment, actor=self.owner)
        mark_assignment_finishing(assignment=self.assignment, actor=self.owner)
        mark_assignment_ready(assignment=self.assignment, actor=self.owner)
        mark_assignment_completed(assignment=self.assignment, actor=self.owner)
        self.assignment.refresh_from_db()
        self.managed_job.refresh_from_db()
        self.production_order.refresh_from_db()
        self.assertEqual(self.assignment.status, "completed")
        self.assertEqual(self.managed_job.status, "completed")
        self.assertEqual(self.production_order.status, "completed")
        self.assertIsNotNone(self.production_order.completed_at)
        self.assertIn("finishing_started_at", self.assignment.operational_snapshot)

    def test_assignment_action_endpoints_and_issue_reporting(self):
        self.client.force_authenticate(user=self.owner)
        accept_response = self.client.post(f"/api/job-assignments/{self.assignment.id}/accept/", {"note": "Accepted"}, format="json")
        self.assertEqual(accept_response.status_code, 200)
        self.assertEqual(accept_response.json()["status"], "accepted")

        issue_response = self.client.post(f"/api/job-assignments/{self.assignment.id}/report-issue/", {"note": "Machine downtime"}, format="json")
        self.assertEqual(issue_response.status_code, 200)
        self.managed_job.refresh_from_db()
        self.assertEqual(self.managed_job.exception_status, "production_issue")
        self.assertTrue(self.managed_job.ops_review_required)

        self.client.force_authenticate(user=self.ops)
        in_production_response = self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-in-production/", {"note": "Printing"}, format="json")
        self.assertEqual(in_production_response.status_code, 200)
        ready_response = self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-ready/", {"note": "Ready"}, format="json")
        self.assertEqual(ready_response.status_code, 200)
        self.assertEqual(ready_response.json()["status"], "ready")

    def test_shop_assignment_list_is_shop_safe(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.get("/api/shop/assignments/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        payload = response.json()[0]
        self.assertEqual(payload["managed_reference"], self.managed_job.managed_reference)
        self.assertIn("workflow_projection", payload)
        self.assertEqual(payload["payout_status_label"], "Waiting for job completion")
        self.assertFalse("client_total" in payload)

    def test_invalid_assignment_transition_returns_400(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-ready/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Cannot mark assignment ready", response.json()["detail"])


class ProductionTimelineTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="timeline-client@test.com", password="pass12345", role="client")
        self.partner = User.objects.create_user(email="timeline-partner@test.com", password="pass12345", role="broker")
        self.owner = User.objects.create_user(email="timeline-owner@test.com", password="pass12345", role="shop_owner")
        self.other_owner = User.objects.create_user(email="timeline-other-owner@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Timeline Shop", slug="timeline-shop", is_active=True)
        self.other_shop = Shop.objects.create(owner=self.other_owner, name="Other Timeline Shop", slug="other-timeline-shop", is_active=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Timeline Customer", email="timeline-client@test.com")
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Timeline Customer",
            customer_email="timeline-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={
                "request_snapshot": {
                    "product_type": "booklet",
                    "product_label": "Booklet",
                    "quantity": 250,
                    "finished_size": "A5",
                    "paper_stock": "Art paper",
                    "print_sides": "Double sided",
                    "color_mode": "Full colour",
                    "lamination": "gloss",
                }
            },
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("1800.00"),
            client_total=Decimal("2600.00"),
            production_base_price=Decimal("1800.00"),
            broker_margin_amount=Decimal("400.00"),
            platform_service_amount=Decimal("400.00"),
        )
        self.managed_job = ManagedJob.objects.create(
            title="Timeline managed job",
            source_quote_request=self.quote_request,
            source_shop_quote=self.shop_quote,
            client=self.client_user,
            customer=self.customer,
            broker=self.partner,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="payment_confirmed",
            payment_status="confirmed",
            assignment_status="assignment_pending",
            client_total=Decimal("2600.00"),
            production_total=Decimal("1800.00"),
            broker_commission=Decimal("400.00"),
            platform_fee=Decimal("400.00"),
        )
        self.assignment = JobAssignment.objects.create(
            managed_job=self.managed_job,
            assigned_shop=self.shop,
            source_shop_quote=self.shop_quote,
            production_amount=Decimal("1800.00"),
            status="pending",
        )

    def test_finishing_status_transition_works_and_payment_confirmed_is_shop_safe(self):
        accept_assignment(assignment=self.assignment, actor=self.owner)
        mark_assignment_in_production(assignment=self.assignment, actor=self.owner)
        mark_assignment_finishing(assignment=self.assignment, actor=self.owner)
        self.assignment.refresh_from_db()

        payload = JobAssignmentSerializer(
            self.assignment,
            context={"request": type("Request", (), {"user": self.owner})()},
        ).data
        self.assertEqual(self.assignment.status, "finishing")
        self.assertTrue(payload["payment_confirmed"])
        self.assertEqual(payload["production_stage"], "finishing")
        self.assertNotIn("client_total", payload)

    def test_assigned_shop_can_progress_valid_states_and_completion_initializes_settlement(self):
        self.client.force_authenticate(user=self.owner)
        self.assertEqual(self.client.post(f"/api/job-assignments/{self.assignment.id}/accept/", {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-in-production/", {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-finishing/", {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-ready/", {}, format="json").status_code, 200)
        completed = self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-completed/", {}, format="json")

        self.assertEqual(completed.status_code, 200)
        self.managed_job.refresh_from_db()
        self.assertEqual(self.managed_job.status, "completed")
        self.assertTrue(JobSettlementSplit.objects.filter(managed_job=self.managed_job).exists())

    def test_invalid_transition_returns_400_and_random_shop_is_blocked(self):
        self.client.force_authenticate(user=self.owner)
        invalid_response = self.client.post(f"/api/job-assignments/{self.assignment.id}/mark-finishing/", {}, format="json")
        self.assertEqual(invalid_response.status_code, 400)
        self.assertIn("start finishing", invalid_response.json()["detail"])

        self.client.force_authenticate(user=self.other_owner)
        forbidden_response = self.client.post(f"/api/job-assignments/{self.assignment.id}/accept/", {}, format="json")
        self.assertEqual(forbidden_response.status_code, 403)


class ManagedJobAuditEventTestCase(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(email="audit-client@test.com", password="pass12345", role="client")
        self.partner = User.objects.create_user(email="audit-partner@test.com", password="pass12345", role="broker")
        self.owner = User.objects.create_user(email="audit-owner@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Audit Shop", slug="audit-shop", is_active=True)
        self.customer = Customer.objects.create(
            shop=self.shop,
            name="Audit Customer",
            email="audit-client@test.com",
            relationship_owner_type=Customer.RelationshipOwnerType.USER,
            relationship_owner_user=self.partner,
        )
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Audit Customer",
            customer_email="audit-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={"visibility": {"topology_mode": "managed", "actor": "client"}},
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("1800.00"),
        )

    def test_events_are_written_for_core_workflow_actions(self):
        from jobs.managed_services import create_managed_job_from_accepted_quote

        managed_job = create_managed_job_from_accepted_quote(
            quote_request=self.quote_request,
            shop_quote=self.shop_quote,
            accepted_by=self.client_user,
        )
        assignment = create_assignment_for_managed_job(managed_job=managed_job, shop_quote=self.shop_quote)
        proof = upload_proof_for_managed_job(
            managed_job=managed_job,
            assignment=assignment,
            uploaded_by=self.owner,
            file=SimpleUploadedFile("audit-proof.pdf", b"proof bytes", content_type="application/pdf"),
            original_filename="audit-proof.pdf",
        )
        approve_job_proof(job_file=proof, actor=self.client_user)
        payment = create_job_payment(
            managed_job=managed_job,
            payer=self.client_user,
            amount=Decimal("1800.00"),
            payment_method="mpesa",
        )
        mark_payment_confirmed(job_payment=payment)
        settlement = initialize_settlement_for_managed_job(managed_job=managed_job)
        mark_settlement_release_ready(settlement=settlement)

        event_types = list(ManagedJobEvent.objects.filter(managed_job=managed_job).values_list("event_type", flat=True))
        self.assertIn("quote_accepted", event_types)
        self.assertIn("managed_job_created", event_types)
        self.assertIn("assignment_created", event_types)
        self.assertIn("file_uploaded", event_types)
        self.assertIn("proof_approved", event_types)
        self.assertIn("payment_confirmed", event_types)
        self.assertIn("settlement_release_ready", event_types)


class ManagedJobVisibilityEndpointsTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="managed-visibility-client@test.com", password="pass12345", role="client")
        self.partner = User.objects.create_user(email="managed-visibility-partner@test.com", password="pass12345", role="broker")
        self.owner = User.objects.create_user(email="managed-visibility-owner@test.com", password="pass12345", role="shop_owner")
        self.ops = User.objects.create_user(email="managed-visibility-ops@test.com", password="pass12345", is_staff=True)
        self.other_client = User.objects.create_user(email="managed-visibility-other@test.com", password="pass12345", role="client")
        self.shop = Shop.objects.create(owner=self.owner, name="Managed Visibility Shop", slug="managed-visibility-shop", is_active=True)
        self.customer = Customer.objects.create(
            shop=self.shop,
            name="Managed Visibility Customer",
            email="managed-visibility-client@test.com",
            relationship_owner_type=Customer.RelationshipOwnerType.USER,
            relationship_owner_user=self.partner,
        )
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Managed Visibility Customer",
            customer_email="managed-visibility-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={"visibility": {"topology_mode": "managed", "actor": "client"}},
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("2200.00"),
        )
        self.managed_job = create_managed_job_from_accepted_quote(
            quote_request=self.quote_request,
            shop_quote=self.shop_quote,
            accepted_by=self.client_user,
        )
        self.managed_job.assigned_shop = self.shop
        self.managed_job.save(update_fields=["assigned_shop", "updated_at"])
        self.assignment = create_assignment_for_managed_job(
            managed_job=self.managed_job,
            shop_quote=self.shop_quote,
        )

    def test_managed_job_list_filters_by_actor_and_masks_client_assignment_identity(self):
        self.client.force_authenticate(user=self.client_user)
        client_response = self.client.get("/api/managed-jobs/")
        self.assertEqual(client_response.status_code, 200)
        self.assertEqual(len(client_response.json()), 1)
        self.assertEqual(client_response.json()[0]["id"], self.managed_job.id)

        self.client.force_authenticate(user=self.partner)
        partner_response = self.client.get("/api/managed-jobs/")
        self.assertEqual(partner_response.status_code, 200)
        self.assertEqual(len(partner_response.json()), 1)

        self.client.force_authenticate(user=self.owner)
        shop_response = self.client.get("/api/managed-jobs/")
        self.assertEqual(shop_response.status_code, 200)
        self.assertEqual(len(shop_response.json()), 1)

        self.client.force_authenticate(user=self.ops)
        ops_response = self.client.get("/api/managed-jobs/")
        self.assertEqual(ops_response.status_code, 200)
        self.assertEqual(len(ops_response.json()), 1)

        self.client.force_authenticate(user=self.other_client)
        other_response = self.client.get("/api/managed-jobs/")
        self.assertEqual(other_response.status_code, 200)
        self.assertEqual(other_response.json(), [])

        client_assignment_payload = JobAssignmentSerializer(
            self.assignment,
            context={"request": type("Request", (), {"user": self.client_user})()},
        ).data
        self.assertEqual(client_assignment_payload["shop_name"], "Verified Print Partner")

    def test_event_endpoint_respects_access_boundary(self):
        events = ManagedJobEvent.objects.filter(managed_job=self.managed_job).order_by("-created_at", "-id")
        self.assertTrue(events.exists())

        self.client.force_authenticate(user=self.client_user)
        client_response = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/events/")
        self.assertEqual(client_response.status_code, 200)
        self.assertEqual(client_response.json()[0]["event_type"], events.first().event_type)

        self.client.force_authenticate(user=self.other_client)
        other_response = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/events/")
        self.assertEqual(other_response.status_code, 403)

    def test_client_can_upload_artwork_and_clear_artwork_required_flag(self):
        self.managed_job.artwork_required = True
        self.managed_job.save(update_fields=["artwork_required", "updated_at"])
        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/files/artwork/",
            {"file": SimpleUploadedFile("client-artwork.pdf", b"artwork", content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        self.managed_job.refresh_from_db()
        self.assertFalse(self.managed_job.artwork_required)
        self.assertTrue(JobFile.objects.filter(managed_job=self.managed_job, file_type="artwork").exists())

        files_response = self.client.get(f"/api/managed-jobs/{self.managed_job.id}/files/")
        self.assertEqual(files_response.status_code, 200)
        self.assertEqual(files_response.json()[0]["original_filename"], "client-artwork.pdf")

    def test_client_artwork_upload_rejects_invalid_file_type(self):
        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(
            f"/api/managed-jobs/{self.managed_job.id}/files/artwork/",
            {"file": SimpleUploadedFile("client-artwork.txt", b"artwork", content_type="text/plain")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Unsupported artwork file type. Upload JPG, PNG, PDF, AI, or EPS.")


class ManagedJobPublicTrackingTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(email="tracking-client@test.com", password="pass12345", role="client")
        self.partner = User.objects.create_user(email="tracking-partner@test.com", password="pass12345", role="partner")
        self.owner = User.objects.create_user(email="tracking-owner@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Tracking Shop", slug="tracking-shop", is_active=True, phone_number="+254700000111")
        self.customer = Customer.objects.create(shop=self.shop, name="Tracking Customer", email="tracking-client@test.com")
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            on_behalf_of=self.client_user,
            customer=self.customer,
            customer_name="Tracking Customer",
            customer_email="tracking-client@test.com",
            status=QuoteStatus.CLOSED,
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("2000.00"),
            estimated_ready_at=timezone.now(),
        )
        self.managed_job = ManagedJob.objects.create(
            title="Managed tracked job",
            source_quote_request=self.quote_request,
            source_shop_quote=self.shop_quote,
            client=self.client_user,
            customer=self.customer,
            broker=self.partner,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="in_production",
            client_total=Decimal("2600.00"),
            production_total=Decimal("2000.00"),
            platform_fee=Decimal("300.00"),
            broker_commission=Decimal("300.00"),
        )

    def test_public_tracking_endpoint_hides_private_shop_and_pricing_fields(self):
        response = self.client.get(f"/api/public/managed-jobs/track/{self.managed_job.tracking_token}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("job_status", payload)
        self.assertIn("partner_name", payload)
        self.assertNotIn("shop_name", payload)
        self.assertNotIn("base_price", payload)
        self.assertNotIn("production_total", payload)
        self.assertNotIn("broker_commission", payload)


class ManagedJobUrgencyFoundationTestCase(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(email="urgency-client@test.com", password="pass12345", role="client")
        self.owner = User.objects.create_user(email="urgency-owner@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Urgency Shop", slug="urgency-shop", is_active=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Urgency Customer", email="urgency-client@test.com")
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer=self.customer,
            customer_name="Urgency Customer",
            customer_email="urgency-client@test.com",
            status=QuoteStatus.CLOSED,
            request_snapshot={
                "visibility": {"topology_mode": "managed", "actor": "client"},
                "request_details": {
                    "urgency_type": "emergency",
                    "requested_deadline": "2026-05-14T21:00:00+03:00",
                },
            },
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuoteStatus.ACCEPTED,
            total=Decimal("1800.00"),
            turnaround_hours=4,
            turnaround_label="Express",
            response_snapshot={
                "urgency_type": "emergency",
                "urgency_multiplier": "1.35",
                "urgency_fee": "180.00",
                "after_hours_fee": "20.00",
            },
        )

    def test_managed_job_and_assignment_capture_urgency_metadata(self):
        managed_job = create_managed_job_from_accepted_quote(
            quote_request=self.quote_request,
            shop_quote=self.shop_quote,
            accepted_by=self.client_user,
        )
        assignment = create_assignment_for_managed_job(managed_job=managed_job, shop_quote=self.shop_quote)

        self.assertEqual(managed_job.urgency_type, "emergency")
        self.assertEqual(str(managed_job.urgency_fee), "180.00")
        self.assertEqual(str(managed_job.after_hours_fee), "20.00")
        self.assertEqual(managed_job.operational_priority_level, 5)
        self.assertIsNotNone(managed_job.requested_deadline)

        self.assertEqual(assignment.urgency_type, "emergency")
        self.assertEqual(assignment.operational_priority_level, 5)
        self.assertEqual(assignment.operational_snapshot["urgency_type"], "emergency")

    def test_workflow_projection_surfaces_priority_without_leaking_formula(self):
        projection = project_workflow_state(
            status="assigned",
            actor="client",
            urgency_type="after_hours",
            operational_priority_level=4,
        )
        self.assertEqual(projection["code"], "after_hours")
        self.assertEqual(projection["tone"], "warning")
        self.assertIn("After-hours", projection["detail"])
        self.assertEqual(projection["priority_level"], 4)


class ManagedJobUrgencySettlementTestCase(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(email="urgency-settlement-client@test.com", password="pass12345", role="client")
        self.partner = User.objects.create_user(email="urgency-settlement-partner@test.com", password="pass12345", role="broker")
        self.owner = User.objects.create_user(email="urgency-settlement-owner@test.com", password="pass12345", role="shop_owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Urgency Settlement Shop", slug="urgency-settlement-shop", is_active=True)
        self.customer = Customer.objects.create(
            shop=self.shop,
            name="Settlement Customer",
            email="urgency-settlement-client@test.com",
            relationship_owner_type=Customer.RelationshipOwnerType.USER,
            relationship_owner_user=self.partner,
        )
        self.managed_job = ManagedJob.objects.create(
            title="Urgent job",
            client=self.client_user,
            customer=self.customer,
            broker=self.partner,
            assigned_shop=self.shop,
            created_by=self.client_user,
            status="awaiting_payment",
            client_total=Decimal("1800.00"),
            production_total=Decimal("1000.00"),
            broker_commission=Decimal("300.00"),
            platform_fee=Decimal("300.00"),
            urgency_type="express",
            urgency_fee=Decimal("180.00"),
            after_hours_fee=Decimal("20.00"),
            operational_priority_level=3,
            relationship_snapshot={"owner_type": "user", "owner_user_id": self.partner.id, "owner_reference": f"user:{self.partner.id}"},
        )

    def test_settlement_allocates_urgency_premium_across_split(self):
        settlement = initialize_settlement_for_managed_job(managed_job=self.managed_job, payment_method="mpesa")
        self.assertEqual(str(settlement.production_amount), "1140.00")
        self.assertEqual(str(settlement.partner_commission), "330.00")
        self.assertEqual(str(settlement.platform_fee), "330.00")
        self.assertEqual(str(settlement.client_total), "1800.00")

    def test_priority_pricing_layers_on_top_of_existing_totals(self):
        pricing = apply_priority_pricing(
            {
                "totals": {"subtotal": "1000.00", "grand_total": "1000.00"},
                "calculation_result": {"line_items": [{"label": "Print and finishing", "amount": "1000.00"}]},
            },
            urgency_type="same_day",
            turnaround_hours=10,
            requested_deadline="2026-05-14T20:00:00+03:00",
        )
        self.assertEqual(pricing["urgency_type"], "same_day")
        self.assertEqual(pricing["operational_priority_level"], 2)
        self.assertEqual(pricing["totals"]["grand_total"], "1200.00")
        labels = [line["label"] for line in pricing["calculation_result"]["line_items"]]
        self.assertIn("Same-Day Turnaround", labels)
        self.assertIn("After-Hours Production", labels)
