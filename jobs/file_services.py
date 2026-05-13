"""Managed job file ownership and visibility helpers."""

from __future__ import annotations

import os
from typing import Any

from django.db import transaction

from api.visibility import CLIENT_ACTOR, OPS_ACTOR, PARTNER_ACTOR, SHOP_ACTOR
from artwork.models import UploadedArtwork
from jobs.audit_services import (
    EVENT_FILE_REPLACED,
    EVENT_FILE_UPLOADED,
    EVENT_PROOF_APPROVED,
    EVENT_PROOF_REJECTED,
    EVENT_REVISION_REQUESTED,
    record_managed_job_event,
)
from jobs.choices import JobFileStatus, JobFileType, JobFileVisibility
from jobs.models import JobAssignment, JobFile, ManagedJob
from quotes.models import QuoteRequest, QuoteRequestAttachment, ShopQuote, ShopQuoteAttachment


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _filename_for_field(file_field: Any) -> str:
    if not file_field:
        return ""
    name = getattr(file_field, "name", "") or str(file_field)
    return os.path.basename(name)


def _stored_name(file_field: Any) -> str | None:
    if not file_field:
        return None
    name = getattr(file_field, "name", "") or None
    return name or None


@transaction.atomic
def create_job_file(
    *,
    managed_job: ManagedJob,
    assignment: JobAssignment | None = None,
    uploaded_by=None,
    file=None,
    original_filename: str = "",
    file_type: str = JobFileType.CUSTOMER_UPLOAD,
    visibility: str = JobFileVisibility.CLIENT,
    status: str = JobFileStatus.UPLOADED,
    version: int = 1,
    notes: str = "",
    replaces: JobFile | None = None,
    source_uploaded_artwork: UploadedArtwork | None = None,
    source_quote_request_attachment: QuoteRequestAttachment | None = None,
    source_shop_quote_attachment: ShopQuoteAttachment | None = None,
) -> JobFile:
    if source_uploaded_artwork:
        existing = JobFile.objects.filter(
            managed_job=managed_job,
            source_uploaded_artwork=source_uploaded_artwork,
        ).first()
        if existing:
            return existing
    if source_quote_request_attachment:
        existing = JobFile.objects.filter(
            managed_job=managed_job,
            source_quote_request_attachment=source_quote_request_attachment,
        ).first()
        if existing:
            return existing
    if source_shop_quote_attachment:
        existing = JobFile.objects.filter(
            managed_job=managed_job,
            source_shop_quote_attachment=source_shop_quote_attachment,
        ).first()
        if existing:
            return existing

    job_file = JobFile.objects.create(
        managed_job=managed_job,
        assignment=assignment,
        uploaded_by=uploaded_by,
        file=file,
        original_filename=original_filename or _filename_for_field(file),
        file_type=file_type,
        visibility=visibility,
        status=status,
        version=version,
        notes=notes,
        replaces=replaces,
        source_uploaded_artwork=source_uploaded_artwork,
        source_quote_request_attachment=source_quote_request_attachment,
        source_shop_quote_attachment=source_shop_quote_attachment,
    )
    record_managed_job_event(
        managed_job=managed_job,
        assignment=assignment,
        job_file=job_file,
        actor=uploaded_by,
        event_type=EVENT_FILE_UPLOADED,
        summary=f"File uploaded: {job_file.original_filename or 'job file'}.",
        metadata={
            "file_type": job_file.file_type,
            "visibility": job_file.visibility,
            "status": job_file.status,
            "version": job_file.version,
        },
    )
    return job_file


def attach_uploaded_artwork_to_managed_job(
    *,
    managed_job: ManagedJob,
    uploaded_artwork: UploadedArtwork,
    assignment: JobAssignment | None = None,
    uploaded_by=None,
    file_type: str = JobFileType.CUSTOMER_UPLOAD,
    visibility: str = JobFileVisibility.CLIENT,
    notes: str = "Imported from calculator artwork upload.",
) -> JobFile:
    return create_job_file(
        managed_job=managed_job,
        assignment=assignment,
        uploaded_by=uploaded_by,
        file=_stored_name(uploaded_artwork.file),
        original_filename=_filename_for_field(uploaded_artwork.file),
        file_type=file_type,
        visibility=visibility,
        notes=notes,
        source_uploaded_artwork=uploaded_artwork,
    )


def _import_quote_request_attachment(*, managed_job: ManagedJob, attachment: QuoteRequestAttachment) -> JobFile:
    return create_job_file(
        managed_job=managed_job,
        uploaded_by=getattr(attachment.quote_request, "created_by", None),
        file=_stored_name(attachment.file),
        original_filename=attachment.name or _filename_for_field(attachment.file),
        file_type=JobFileType.CUSTOMER_UPLOAD,
        visibility=JobFileVisibility.CLIENT,
        notes="Imported from legacy quote request attachment.",
        source_quote_request_attachment=attachment,
    )


def _import_shop_quote_attachment(*, managed_job: ManagedJob, attachment: ShopQuoteAttachment) -> JobFile:
    return create_job_file(
        managed_job=managed_job,
        uploaded_by=getattr(attachment.shop_quote, "created_by", None),
        file=_stored_name(attachment.file),
        original_filename=attachment.name or _filename_for_field(attachment.file),
        file_type=JobFileType.PROOF,
        visibility=JobFileVisibility.SHOP,
        notes="Imported from legacy shop quote attachment.",
        source_shop_quote_attachment=attachment,
    )


def import_legacy_files_to_managed_job(
    *,
    managed_job: ManagedJob,
    quote_request: QuoteRequest | None = None,
    shop_quote: ShopQuote | None = None,
) -> list[JobFile]:
    imported: list[JobFile] = []
    resolved_quote_request = quote_request or managed_job.source_quote_request
    resolved_shop_quote = shop_quote or managed_job.source_shop_quote

    if resolved_quote_request:
        for attachment in resolved_quote_request.attachments.all():
            imported.append(_import_quote_request_attachment(managed_job=managed_job, attachment=attachment))

        request_snapshot = _as_dict(getattr(resolved_quote_request, "request_snapshot", None))
        custom_snapshot = _as_dict(request_snapshot.get("custom_product_snapshot"))
        for ref in _as_list(custom_snapshot.get("artwork_refs")):
            ref_payload = _as_dict(ref)
            artwork_id = ref_payload.get("artwork_id") or ref_payload.get("id")
            if not artwork_id:
                continue
            artwork = UploadedArtwork.objects.filter(pk=artwork_id).first()
            if artwork:
                imported.append(
                    attach_uploaded_artwork_to_managed_job(
                        managed_job=managed_job,
                        uploaded_artwork=artwork,
                        uploaded_by=managed_job.client or managed_job.created_by,
                    )
                )

    if resolved_shop_quote:
        active_assignment = managed_job.assignments.filter(reassigned_from__isnull=True).first()
        for attachment in resolved_shop_quote.attachments.all():
            job_file = _import_shop_quote_attachment(managed_job=managed_job, attachment=attachment)
            if active_assignment and job_file.assignment_id != active_assignment.id:
                job_file.assignment = active_assignment
                job_file.save(update_fields=["assignment", "updated_at"])
            imported.append(job_file)

    return imported


def get_visible_job_files_for_actor(
    *,
    managed_job: ManagedJob,
    actor: str,
    assignment: JobAssignment | None = None,
):
    queryset = JobFile.objects.filter(managed_job=managed_job).select_related(
        "assignment",
        "uploaded_by",
        "source_uploaded_artwork",
        "source_quote_request_attachment",
        "source_shop_quote_attachment",
    )
    if assignment:
        queryset = queryset.filter(assignment=assignment) | queryset.filter(assignment__isnull=True)

    if actor == OPS_ACTOR:
        return queryset.distinct().order_by("created_at", "id")
    if actor == SHOP_ACTOR:
        return queryset.filter(
            visibility__in=[
                JobFileVisibility.CLIENT,
                JobFileVisibility.PARTNER,
                JobFileVisibility.SHOP,
            ]
        ).distinct().order_by("created_at", "id")
    if actor == PARTNER_ACTOR:
        return queryset.filter(
            visibility__in=[
                JobFileVisibility.CLIENT,
                JobFileVisibility.PARTNER,
                JobFileVisibility.SHOP,
            ]
        ).exclude(file_type=JobFileType.DELIVERY_EVIDENCE).distinct().order_by("created_at", "id")
    if actor == CLIENT_ACTOR:
        return queryset.filter(
            visibility__in=[
                JobFileVisibility.CLIENT,
                JobFileVisibility.PARTNER,
            ],
            file_type__in=[
                JobFileType.CUSTOMER_UPLOAD,
                JobFileType.PROOF,
                JobFileType.DELIVERY_EVIDENCE,
            ],
        ).distinct().order_by("created_at", "id")
    return queryset.none()


@transaction.atomic
def mark_job_file_replaced(*, job_file: JobFile, replacement: JobFile | None = None) -> JobFile:
    job_file.status = JobFileStatus.REPLACED
    job_file.save(update_fields=["status", "updated_at"])
    record_managed_job_event(
        managed_job=job_file.managed_job,
        assignment=job_file.assignment,
        job_file=job_file,
        actor=getattr(replacement, "uploaded_by", None),
        event_type=EVENT_FILE_REPLACED,
        summary=f"File replaced: {job_file.original_filename or 'job file'}.",
        metadata={"replacement_id": getattr(replacement, "id", None)},
    )
    if replacement and replacement.replaces_id != job_file.id:
        replacement.replaces = job_file
        replacement.version = max(job_file.version + 1, replacement.version)
        replacement.save(update_fields=["replaces", "version", "updated_at"])
    return job_file


def create_print_ready_file(
    *,
    managed_job: ManagedJob,
    assignment: JobAssignment | None = None,
    uploaded_by=None,
    file=None,
    original_filename: str = "",
    notes: str = "Print-ready production file.",
    replaces: JobFile | None = None,
) -> JobFile:
    version = (replaces.version + 1) if replaces else 1
    return create_job_file(
        managed_job=managed_job,
        assignment=assignment,
        uploaded_by=uploaded_by,
        file=file,
        original_filename=original_filename,
        file_type=JobFileType.PRINT_READY,
        visibility=JobFileVisibility.SHOP,
        status=JobFileStatus.APPROVED,
        version=version,
        notes=notes,
        replaces=replaces,
    )


@transaction.atomic
def upload_proof_for_managed_job(
    *,
    managed_job: ManagedJob,
    assignment: JobAssignment | None = None,
    uploaded_by=None,
    file=None,
    original_filename: str = "",
    notes: str = "Proof uploaded for approval.",
) -> JobFile:
    return create_job_file(
        managed_job=managed_job,
        assignment=assignment,
        uploaded_by=uploaded_by,
        file=file,
        original_filename=original_filename,
        file_type=JobFileType.PROOF,
        visibility=JobFileVisibility.PARTNER,
        status=JobFileStatus.PROOF_UPLOADED,
        notes=notes,
    )


@transaction.atomic
def approve_job_proof(*, job_file: JobFile, actor=None, notes: str = "") -> JobFile:
    job_file.status = JobFileStatus.PROOF_APPROVED
    if notes:
        job_file.notes = notes
        job_file.save(update_fields=["status", "notes", "updated_at"])
    else:
        job_file.save(update_fields=["status", "updated_at"])
    record_managed_job_event(
        managed_job=job_file.managed_job,
        assignment=job_file.assignment,
        job_file=job_file,
        actor=actor,
        event_type=EVENT_PROOF_APPROVED,
        summary=f"Proof approved: {job_file.original_filename or 'proof file'}.",
        metadata={"status": job_file.status},
    )
    return job_file


@transaction.atomic
def reject_job_proof(*, job_file: JobFile, actor=None, notes: str = "") -> JobFile:
    job_file.status = JobFileStatus.PROOF_REJECTED
    if notes:
        job_file.notes = notes
        job_file.save(update_fields=["status", "notes", "updated_at"])
    else:
        job_file.save(update_fields=["status", "updated_at"])
    record_managed_job_event(
        managed_job=job_file.managed_job,
        assignment=job_file.assignment,
        job_file=job_file,
        actor=actor,
        event_type=EVENT_PROOF_REJECTED,
        summary=f"Proof rejected: {job_file.original_filename or 'proof file'}.",
        metadata={"status": job_file.status},
    )
    return job_file


@transaction.atomic
def request_revision(*, job_file: JobFile, actor=None, notes: str = "") -> JobFile:
    job_file.status = JobFileStatus.REVISION_REQUESTED
    if notes:
        job_file.notes = notes
        job_file.save(update_fields=["status", "notes", "updated_at"])
    else:
        job_file.save(update_fields=["status", "updated_at"])
    record_managed_job_event(
        managed_job=job_file.managed_job,
        assignment=job_file.assignment,
        job_file=job_file,
        actor=actor,
        event_type=EVENT_REVISION_REQUESTED,
        summary=f"Revision requested for {job_file.original_filename or 'proof file'}.",
        metadata={"status": job_file.status},
    )
    return job_file


@transaction.atomic
def mark_file_print_ready(*, job_file: JobFile, actor=None, notes: str = "") -> JobFile:
    job_file.file_type = JobFileType.PRINT_READY
    job_file.visibility = JobFileVisibility.SHOP
    job_file.status = JobFileStatus.PRINT_READY
    update_fields = ["file_type", "visibility", "status", "updated_at"]
    if notes:
        job_file.notes = notes
        update_fields.append("notes")
    job_file.save(update_fields=update_fields)
    record_managed_job_event(
        managed_job=job_file.managed_job,
        assignment=job_file.assignment,
        job_file=job_file,
        actor=actor,
        event_type=EVENT_FILE_UPLOADED,
        summary=f"File marked print ready: {job_file.original_filename or 'job file'}.",
        metadata={"status": job_file.status, "file_type": job_file.file_type},
    )
    return job_file
