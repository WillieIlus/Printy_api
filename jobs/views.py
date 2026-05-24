"""JobShare API views."""
from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.visibility import CLIENT_ACTOR, OPS_ACTOR, PARTNER_ACTOR, SHOP_ACTOR, resolve_actor
from api.filters import JobRequestFilterSet
from jobs.assignment_services import (
    accept_assignment,
    mark_assignment_finishing,
    mark_assignment_completed,
    mark_assignment_in_production,
    mark_assignment_ready,
    reject_assignment,
    report_assignment_issue,
)
from jobs.file_services import (
    approve_job_proof,
    get_visible_job_files_for_actor,
    mark_file_print_ready,
    reject_job_proof,
    request_revision,
    sync_managed_job_artwork_requirement,
    upload_artwork_for_managed_job,
    upload_proof_for_managed_job,
)
from jobs.formatter import format_job_for_whatsapp_share
from jobs.models import JobAssignment, JobClaim, JobFile, JobNotification, JobPayment, JobRequest, ManagedJob
from jobs.payment_services import (
    initialize_settlement_for_managed_job,
    initiate_job_stk_push,
    reconcile_job_payment_status,
)
from jobs.serializers import (
    JobActionSerializer,
    JobAssignmentSerializer,
    JobPaymentQuerySerializer,
    JobClaimCreateSerializer,
    JobClaimSerializer,
    JobFileSerializer,
    ManagedJobStkInitiateSerializer,
    ManagedJobEventSerializer,
    ManagedJobPublicTrackingSerializer,
    ManagedJobSerializer,
    JobPaymentSerializer,
    JobRequestCreateSerializer,
    JobRequestDetailSerializer,
    JobRequestListSerializer,
    JobRequestPublicSerializer,
    JobSettlementSplitSerializer,
)


class JobRequestViewSet(viewsets.ModelViewSet):
    """
    JobShare API.
    POST /api/job-requests/ — create (authenticated printer/staff)
    GET /api/job-requests/?status=OPEN — list
    GET /api/job-requests/{id}/ — detail
    POST /api/job-requests/{id}/whatsapp-share/ — shareable message + public_view_url
    """

    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = JobRequestFilterSet

    def get_queryset(self):
        return JobRequest.objects.select_related("created_by").prefetch_related(
            "claims"
        ).order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return JobRequestCreateSerializer
        if self.action in ("list",):
            return JobRequestListSerializer
        return JobRequestDetailSerializer

    def create(self, request, *args, **kwargs):
        from rest_framework import status
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            JobRequestDetailSerializer(serializer.instance).data,
            status=status.HTTP_201_CREATED,
        )

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"], url_path="whatsapp-share")
    def whatsapp_share(self, request, pk=None):
        """Returns shareable message + public_view_url (tokenized)."""
        job = self.get_object()
        job.ensure_public_token()
        message = format_job_for_whatsapp_share(job)
        frontend_url = getattr(settings, "FRONTEND_URL", "https://printy.ke")
        public_view_url = f"{frontend_url.rstrip('/')}/track-job/{job.public_token}"
        return Response({
            "message": message,
            "public_view_url": public_view_url,
        })

    @action(detail=True, methods=["post"], url_path="claims")
    def create_claim(self, request, pk=None):
        """POST /api/job-requests/{id}/claims/ — create a claim (only OPEN jobs)."""
        job = self.get_object()
        if job.status != JobRequest.OPEN:
            return Response(
                {"detail": _("Only open jobs can be claimed.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if job.created_by_id == request.user.id:
            return Response(
                {"detail": _("You cannot claim your own job.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = JobClaimCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claim, created = JobClaim.objects.get_or_create(
            job_request=job,
            claimed_by=request.user,
            defaults={
                "price_offered": serializer.validated_data.get("price_offered"),
                "message": serializer.validated_data.get("message", ""),
            },
        )
        if not created:
            return Response(
                {"detail": _("You have already claimed this job.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            JobClaimSerializer(claim).data,
            status=status.HTTP_201_CREATED,
        )


class JobClaimViewSet(viewsets.ReadOnlyModelViewSet):
    """
    JobClaim API.
    GET /api/job-claims/?claimed_by=me — list (filter by claimed_by)
    GET /api/job-claims/{id}/ — retrieve claim
    POST /api/job-claims/{id}/accept/ — job owner accepts (marks job CLAIMED, creates notification)
    POST /api/job-claims/{id}/reject/ — job owner rejects
    """

    permission_classes = [IsAuthenticated]
    serializer_class = JobClaimSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["job_request", "status"]

    def get_queryset(self):
        qs = JobClaim.objects.select_related("job_request", "claimed_by").order_by("-created_at")
        if self.request.query_params.get("claimed_by") == "me":
            qs = qs.filter(claimed_by=self.request.user)
        return qs

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        """Job owner accepts claim. Marks job CLAIMED, creates notification."""
        claim = self.get_object()
        if claim.job_request.created_by_id != request.user.id:
            return Response(
                {"detail": _("Only the job owner can accept claims.")},
                status=status.HTTP_403_FORBIDDEN,
            )
        if claim.status != JobClaim.PENDING:
            return Response(
                {"detail": _("Claim is no longer pending.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        claim.status = JobClaim.ACCEPTED
        claim.save(update_fields=["status", "updated_at"])
        claim.job_request.status = JobRequest.CLAIMED
        claim.job_request.save(update_fields=["status", "updated_at"])
        JobNotification.objects.create(
            user=claim.claimed_by,
            job_request=claim.job_request,
            job_claim=claim,
            message=_("Your claim on '%(title)s' was accepted!") % {"title": claim.job_request.title},
        )
        return Response(JobClaimSerializer(claim).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """Job owner rejects claim."""
        claim = self.get_object()
        if claim.job_request.created_by_id != request.user.id:
            return Response(
                {"detail": _("Only the job owner can reject claims.")},
                status=status.HTTP_403_FORBIDDEN,
            )
        if claim.status != JobClaim.PENDING:
            return Response(
                {"detail": _("Claim is no longer pending.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        claim.status = JobClaim.REJECTED
        claim.save(update_fields=["status", "updated_at"])
        return Response(JobClaimSerializer(claim).data)


class PublicJobView(APIView):
    """
    GET /api/public/job/{token}/ — minimal read-only info for public share.
    No auth required. Token must be valid.
    """

    permission_classes = [AllowAny]

    def get(self, request, token):
        job = get_object_or_404(JobRequest, public_token=token)
        serializer = JobRequestPublicSerializer(job)
        data = serializer.data
        # Add CTA hint
        data["claim_cta"] = _("Claim job")
        data["requires_login"] = True
        return Response(data)


class PublicManagedJobTrackingView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, token):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("broker", "broker__profile", "source_shop_quote"),
            tracking_token=token,
        )
        serializer = ManagedJobPublicTrackingSerializer(managed_job, context={"request": request})
        return Response(serializer.data)


def _can_access_managed_job(*, user, managed_job: ManagedJob, actor: str) -> bool:
    if actor == OPS_ACTOR:
        return True
    if actor == SHOP_ACTOR:
        if managed_job.assigned_shop_id and getattr(managed_job.assigned_shop, "owner_id", None) == user.id:
            return True
        return managed_job.assignments.filter(
            reassigned_from__isnull=True,
            assigned_shop__owner=user,
        ).exists()
    if actor == PARTNER_ACTOR:
        return managed_job.broker_id == user.id
    return managed_job.client_id == user.id or managed_job.created_by_id == user.id


def _can_manage_assignment(*, user, assignment: JobAssignment, actor: str) -> bool:
    if actor == OPS_ACTOR:
        return True
    if actor == SHOP_ACTOR:
        if assignment.assigned_shop_id and getattr(assignment.assigned_shop, "owner_id", None) == user.id:
            return True
        return False
    return False


class ManagedJobFileListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        files = get_visible_job_files_for_actor(managed_job=managed_job, actor=actor)
        return Response(JobFileSerializer(files, many=True, context={"request": request}).data)


class ManagedJobArtworkUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        if actor not in {OPS_ACTOR, CLIENT_ACTOR, PARTNER_ACTOR}:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": _("An artwork file is required.")}, status=status.HTTP_400_BAD_REQUEST)
        assignment = managed_job.assignments.filter(reassigned_from__isnull=True).first()
        job_file = upload_artwork_for_managed_job(
            managed_job=managed_job,
            assignment=assignment,
            uploaded_by=request.user,
            file=upload,
            original_filename=getattr(upload, "name", ""),
            notes=request.data.get("note", "") or "Artwork uploaded for production.",
        )
        return Response(JobFileSerializer(job_file, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ManagedJobListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        actor = resolve_actor(request.user)
        queryset = ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by").prefetch_related("job_files", "payments")
        if actor == OPS_ACTOR:
            items = queryset.order_by("-operational_priority_level", "-created_at")
        elif actor == SHOP_ACTOR:
            items = queryset.filter(assigned_shop__owner=request.user).order_by("-operational_priority_level", "-created_at")
        elif actor == PARTNER_ACTOR:
            items = queryset.filter(broker=request.user).order_by("-operational_priority_level", "-created_at")
        else:
            items = queryset.filter(client=request.user).order_by("-operational_priority_level", "-created_at")
        return Response(ManagedJobSerializer(items, many=True, context={"request": request}).data)


class ManagedJobPaymentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        payments = JobPayment.objects.filter(managed_job=managed_job).order_by("-created_at")
        return Response(JobPaymentSerializer(payments, many=True, context={"request": request}).data)


class ManagedJobStkPushView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if actor not in {CLIENT_ACTOR, OPS_ACTOR}:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        if actor != OPS_ACTOR and not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)

        serializer = ManagedJobStkInitiateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payment = initiate_job_stk_push(
                managed_job=managed_job,
                payer=request.user,
                phone_number=serializer.validated_data["phone_number"],
                amount=serializer.validated_data.get("amount"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response(
                {"detail": _("Failed to initiate payment. Please try again.")},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(JobPaymentSerializer(payment, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ManagedJobPaymentQueryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        managed_job = get_object_or_404(ManagedJob, pk=pk)
        actor = resolve_actor(request.user)
        if actor != OPS_ACTOR:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)

        serializer = JobPaymentQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = get_object_or_404(
            JobPayment.objects.filter(managed_job=managed_job),
            checkout_request_id=serializer.validated_data["checkout_request_id"],
        )
        try:
            reconcile_job_payment_status(job_payment=payment)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({"detail": _("Failed to query payment status.")}, status=status.HTTP_502_BAD_GATEWAY)
        payment.refresh_from_db()
        return Response(JobPaymentSerializer(payment, context={"request": request}).data)


class ManagedJobSettlementDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        settlement = getattr(managed_job, "settlement_split", None)
        if settlement is None:
            settlement = initialize_settlement_for_managed_job(managed_job=managed_job)
        return Response(JobSettlementSplitSerializer(settlement, context={"request": request}).data)


class ManagedJobEventListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        events = managed_job.events.select_related("actor").order_by("-created_at", "-id")[:50]
        return Response(ManagedJobEventSerializer(events, many=True).data)


class ManagedJobProofUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        managed_job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop", "client", "broker", "created_by"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        if actor not in {OPS_ACTOR, SHOP_ACTOR, PARTNER_ACTOR}:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": _("A proof file is required.")}, status=status.HTTP_400_BAD_REQUEST)
        job_file = upload_proof_for_managed_job(
            managed_job=managed_job,
            assignment=managed_job.assignments.filter(reassigned_from__isnull=True).first(),
            uploaded_by=request.user,
            file=upload,
            original_filename=getattr(upload, "name", ""),
            notes=request.data.get("note", "") or "Proof uploaded for approval.",
        )
        return Response(JobFileSerializer(job_file, context={"request": request}).data, status=status.HTTP_201_CREATED)


class JobFileActionView(APIView):
    permission_classes = [IsAuthenticated]
    action_name = ""

    def post(self, request, pk):
        job_file = get_object_or_404(
            JobFile.objects.select_related("managed_job__assigned_shop", "managed_job__client", "managed_job__broker", "managed_job__created_by", "assignment"),
            pk=pk,
        )
        managed_job = job_file.managed_job
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)

        serializer = JobActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.validated_data.get("note", "")

        if self.action_name in {"approve", "reject", "revision"} and actor not in {OPS_ACTOR, CLIENT_ACTOR, PARTNER_ACTOR}:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        if self.action_name == "print_ready" and actor not in {OPS_ACTOR, SHOP_ACTOR}:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)

        if self.action_name == "approve":
            job_file = approve_job_proof(job_file=job_file, actor=request.user, notes=note)
        elif self.action_name == "reject":
            job_file = reject_job_proof(job_file=job_file, actor=request.user, notes=note)
        elif self.action_name == "revision":
            job_file = request_revision(job_file=job_file, actor=request.user, notes=note)
        else:
            job_file = mark_file_print_ready(job_file=job_file, actor=request.user, notes=note)
        return Response(JobFileSerializer(job_file, context={"request": request}).data)


class JobFileApproveView(JobFileActionView):
    action_name = "approve"


class JobFileRejectView(JobFileActionView):
    action_name = "reject"


class JobFileRevisionView(JobFileActionView):
    action_name = "revision"


class JobFilePrintReadyView(JobFileActionView):
    action_name = "print_ready"


class ShopAssignmentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        actor = resolve_actor(request.user)
        if actor not in {OPS_ACTOR, SHOP_ACTOR}:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        queryset = JobAssignment.objects.select_related("managed_job", "assigned_shop", "production_order").filter(reassigned_from__isnull=True)
        if actor == SHOP_ACTOR:
            queryset = queryset.filter(assigned_shop__owner=request.user)
        return Response(JobAssignmentSerializer(queryset.order_by("-operational_priority_level", "-created_at"), many=True, context={"request": request}).data)


class JobAssignmentActionView(APIView):
    permission_classes = [IsAuthenticated]
    action_name = ""

    def post(self, request, pk):
        assignment = get_object_or_404(
            JobAssignment.objects.select_related("managed_job", "assigned_shop", "production_order"),
            pk=pk,
        )
        actor = resolve_actor(request.user)
        if not _can_manage_assignment(user=request.user, assignment=assignment, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        serializer = JobActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.validated_data.get("note", "")
        try:
            if self.action_name == "accept":
                assignment = accept_assignment(assignment=assignment, actor=request.user, note=note)
            elif self.action_name == "reject":
                assignment = reject_assignment(assignment=assignment, actor=request.user, note=note)
            elif self.action_name == "in_production":
                assignment = mark_assignment_in_production(assignment=assignment, actor=request.user, note=note)
            elif self.action_name == "finishing":
                assignment = mark_assignment_finishing(assignment=assignment, actor=request.user, note=note)
            elif self.action_name == "ready":
                assignment = mark_assignment_ready(assignment=assignment, actor=request.user, note=note)
            elif self.action_name == "completed":
                assignment = mark_assignment_completed(assignment=assignment, actor=request.user, note=note)
            else:
                assignment = report_assignment_issue(assignment=assignment, actor=request.user, note=note)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        sync_managed_job_artwork_requirement(managed_job=assignment.managed_job)
        return Response(JobAssignmentSerializer(assignment, context={"request": request}).data)


class JobAssignmentAcceptView(JobAssignmentActionView):
    action_name = "accept"


class JobAssignmentRejectView(JobAssignmentActionView):
    action_name = "reject"


class JobAssignmentInProductionView(JobAssignmentActionView):
    action_name = "in_production"


class JobAssignmentFinishingView(JobAssignmentActionView):
    action_name = "finishing"


class JobAssignmentReadyView(JobAssignmentActionView):
    action_name = "ready"


class JobAssignmentCompletedView(JobAssignmentActionView):
    action_name = "completed"


class JobAssignmentIssueView(JobAssignmentActionView):
    action_name = "issue"


class JobFileDownloadView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        job_file = get_object_or_404(
            JobFile.objects.select_related(
                "managed_job__assigned_shop",
                "managed_job__client",
                "managed_job__broker",
                "managed_job__created_by",
            ),
            pk=pk,
        )
        managed_job = job_file.managed_job
        actor = resolve_actor(request.user)
        if not _can_access_managed_job(user=request.user, managed_job=managed_job, actor=actor):
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        visible_ids = set(
            get_visible_job_files_for_actor(managed_job=managed_job, actor=actor).values_list("id", flat=True)
        )
        if job_file.id not in visible_ids:
            return Response({"detail": _("Not authorized.")}, status=status.HTTP_403_FORBIDDEN)
        if not job_file.file:
            return Response({"detail": _("File is not available for download.")}, status=status.HTTP_404_NOT_FOUND)
        return FileResponse(
            job_file.file.open("rb"),
            as_attachment=True,
            filename=job_file.original_filename or job_file.file.name.rsplit("/", 1)[-1],
        )
