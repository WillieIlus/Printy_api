from rest_framework import serializers
from decimal import Decimal

from catalog.models import Product
from inventory.models import Machine, Paper
from pricing.models import FinishingRate, Material
from api.size_utils import normalize_size_payload, validate_size_selection
from quotes.choices import QuoteDraftStatus, QuoteStatus, ShopQuoteStatus
from quotes.models import QuoteDraft, QuoteRequest, ShopQuote
from quotes.request_brief import build_quote_request_whatsapp_handoff
from quotes.status_normalization import (
    denormalize_quote_response_status,
    normalize_quote_draft_status,
    normalize_quote_request_status,
    normalize_quote_response_status,
    quote_draft_status_label,
    quote_request_status_label,
    quote_response_status_label,
)
from quotes.turnaround import estimate_turnaround, legacy_days_from_hours
from shops.models import Shop


class FinishingSelectionSerializer(serializers.Serializer):
    finishing_rate = serializers.PrimaryKeyRelatedField(queryset=FinishingRate.objects.filter(is_active=True))
    selected_side = serializers.ChoiceField(choices=["front", "back", "both"], default="both")

    def to_internal_value(self, data):
        if isinstance(data, dict) and "finishing_rate" not in data and "finishing_rate_id" in data:
            data = {**data, "finishing_rate": data["finishing_rate_id"]}
        return super().to_internal_value(data)


class CalculatorConfigPreviewSerializer(serializers.Serializer):
    product_type = serializers.ChoiceField(
        choices=["business_card", "flyer", "label_sticker", "letterhead", "booklet"],
        help_text="Homepage calculator product preset to preview.",
    )
    quantity = serializers.IntegerField(required=False, allow_null=True, min_value=1, default=100, help_text="Requested quantity.")
    finished_size = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Use values from /api/calculator/config/, e.g. 85x55mm, A5, A4.")
    print_sides = serializers.ChoiceField(choices=["SIMPLEX", "DUPLEX"], required=False, allow_null=True, default="SIMPLEX", help_text="Flat-job print sides.")
    color_mode = serializers.ChoiceField(choices=["BW", "COLOR"], required=False, allow_null=True, default="COLOR", help_text="Flat-job colour mode.")
    paper_stock = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Paper stock key from /api/calculator/config/.")
    requested_paper_category = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Fallback paper category when the buyer wants the shop to advise.")
    requested_gsm = serializers.IntegerField(required=False, allow_null=True, min_value=1, help_text="Preferred paper gsm.")
    lamination = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Finishing slug such as gloss-lamination or matt-lamination.")
    corner_rounding = serializers.BooleanField(required=False, allow_null=True, help_text="Business-card corner rounding request.")
    folding = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Optional folding preference for flyers.")
    shape = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Sticker shape.")
    cut_type = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Sticker cut type.")
    total_pages = serializers.IntegerField(required=False, allow_null=True, min_value=1, help_text="Booklet page count before normalization.")
    cover_stock = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Booklet cover stock key from /api/calculator/config/.")
    insert_stock = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Booklet insert stock key from /api/calculator/config/.")
    requested_cover_paper_category = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Fallback cover paper category.")
    requested_cover_gsm = serializers.IntegerField(required=False, allow_null=True, min_value=1, help_text="Fallback cover paper gsm.")
    requested_insert_paper_category = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Fallback insert paper category.")
    requested_insert_gsm = serializers.IntegerField(required=False, allow_null=True, min_value=1, help_text="Fallback insert paper gsm.")
    cover_lamination = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Booklet cover lamination mode.")
    binding_type = serializers.CharField(required=False, allow_blank=True, allow_null=True, help_text="Booklet binding type, e.g. saddle_stitch.")
    cutting = serializers.BooleanField(required=False, allow_null=True, help_text="Whether booklet cutting is requested.")
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1, help_text="Optional turnaround target in working hours.")


class CalculatorPreviewSerializer(serializers.Serializer):
    shop = serializers.PrimaryKeyRelatedField(queryset=Shop.objects.all())
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), required=False, allow_null=True)
    quantity = serializers.IntegerField(min_value=1)
    paper = serializers.PrimaryKeyRelatedField(queryset=Paper.objects.filter(is_active=True))
    machine = serializers.PrimaryKeyRelatedField(queryset=Machine.objects.filter(is_active=True))
    color_mode = serializers.ChoiceField(choices=["BW", "COLOR"], default="COLOR")
    sides = serializers.ChoiceField(choices=["SIMPLEX", "DUPLEX"], default="SIMPLEX")
    apply_duplex_surcharge = serializers.BooleanField(required=False, allow_null=True, default=None)
    size_mode = serializers.ChoiceField(choices=["standard", "custom"], required=False, default="custom")
    size_label = serializers.CharField(required=False, allow_blank=True, default="")
    input_unit = serializers.ChoiceField(choices=["mm", "cm", "m", "in"], required=False, default="mm")
    width_input = serializers.DecimalField(required=False, allow_null=True, max_digits=10, decimal_places=3, min_value=Decimal("0.001"))
    height_input = serializers.DecimalField(required=False, allow_null=True, max_digits=10, decimal_places=3, min_value=Decimal("0.001"))
    width_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    height_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    finishings = FinishingSelectionSerializer(many=True, required=False)

    def to_internal_value(self, data):
        normalized = normalize_size_payload(
            data,
            legacy_width_keys=("chosen_width_mm",),
            legacy_height_keys=("chosen_height_mm",),
        )
        return super().to_internal_value(normalized)

    def validate(self, attrs):
        attrs = validate_size_selection(attrs)
        shop = attrs["shop"]
        product = attrs.get("product")
        errors = {}

        if product and product.shop_id != shop.id:
            errors["product"] = ["Product must belong to the selected shop."]
        if not product and (not attrs.get("width_mm") or not attrs.get("height_mm")):
            errors["non_field_errors"] = ["width_mm and height_mm are required for custom previews."]

        if attrs["paper"].shop_id != shop.id:
            errors["paper"] = ["Paper must belong to the selected shop."]
        if attrs["machine"].shop_id != shop.id:
            errors["machine"] = ["Machine must belong to the selected shop."]

        finishing_errors = []
        for selection in attrs.get("finishings") or []:
            if selection["finishing_rate"].shop_id != shop.id:
                finishing_errors.append(
                    {"finishing_rate": ["Finishing rate must belong to the selected shop."]}
                )
            else:
                finishing_errors.append({})
        if any(item for item in finishing_errors):
            errors["finishings"] = finishing_errors

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class BookletCalculatorPreviewSerializer(serializers.Serializer):
    shop = serializers.PrimaryKeyRelatedField(queryset=Shop.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    total_pages = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    binding_type = serializers.ChoiceField(choices=["saddle_stitch", "perfect_bind", "wire_o"], default="saddle_stitch")
    cover_paper = serializers.PrimaryKeyRelatedField(queryset=Paper.objects.filter(is_active=True), required=False, allow_null=True)
    insert_paper = serializers.PrimaryKeyRelatedField(queryset=Paper.objects.filter(is_active=True), required=False, allow_null=True)
    cover_sides = serializers.ChoiceField(choices=["SIMPLEX", "DUPLEX"], default="DUPLEX")
    insert_sides = serializers.ChoiceField(choices=["SIMPLEX", "DUPLEX"], default="DUPLEX")
    cover_color_mode = serializers.ChoiceField(choices=["BW", "COLOR"], default="COLOR")
    insert_color_mode = serializers.ChoiceField(choices=["BW", "COLOR"], default="COLOR")
    cover_lamination_mode = serializers.ChoiceField(choices=["none", "front", "both"], default="none")
    cover_lamination_finishing_rate = serializers.PrimaryKeyRelatedField(
        queryset=FinishingRate.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    finishings = FinishingSelectionSerializer(many=True, required=False)
    binding_finishing_rate = serializers.PrimaryKeyRelatedField(
        queryset=FinishingRate.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    size_mode = serializers.ChoiceField(choices=["standard", "custom"], required=False, default="custom")
    size_label = serializers.CharField(required=False, allow_blank=True, default="")
    input_unit = serializers.ChoiceField(choices=["mm", "cm", "m", "in"], required=False, default="mm")
    width_input = serializers.DecimalField(required=False, allow_null=True, max_digits=10, decimal_places=3, min_value=Decimal("0.001"))
    height_input = serializers.DecimalField(required=False, allow_null=True, max_digits=10, decimal_places=3, min_value=Decimal("0.001"))
    width_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    height_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def to_internal_value(self, data):
        normalized = normalize_size_payload(
            data,
            legacy_width_keys=("chosen_width_mm",),
            legacy_height_keys=("chosen_height_mm",),
        )
        return super().to_internal_value(normalized)

    def validate(self, attrs):
        attrs = validate_size_selection(attrs)
        shop = attrs["shop"]
        errors = {}
        cover_paper = attrs.get("cover_paper")
        insert_paper = attrs.get("insert_paper")
        if cover_paper and cover_paper.shop_id != shop.id:
            errors["cover_paper"] = ["Cover paper must belong to the selected shop."]
        if insert_paper and insert_paper.shop_id != shop.id:
            errors["insert_paper"] = ["Insert paper must belong to the selected shop."]
        if attrs.get("cover_lamination_finishing_rate") and attrs["cover_lamination_finishing_rate"].shop_id != shop.id:
            errors["cover_lamination_finishing_rate"] = ["Lamination rate must belong to the selected shop."]
        if attrs.get("binding_finishing_rate") and attrs["binding_finishing_rate"].shop_id != shop.id:
            errors["binding_finishing_rate"] = ["Binding rate must belong to the selected shop."]
        finishing_errors = []
        for selection in attrs.get("finishings") or []:
            if selection["finishing_rate"].shop_id != shop.id:
                finishing_errors.append(
                    {"finishing_rate": ["Finishing rate must belong to the selected shop."]}
                )
            else:
                finishing_errors.append({})
        if any(item for item in finishing_errors):
            errors["finishings"] = finishing_errors
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class LargeFormatCalculatorPreviewSerializer(serializers.Serializer):
    shop = serializers.PrimaryKeyRelatedField(queryset=Shop.objects.all())
    product_subtype = serializers.ChoiceField(
        choices=["banner", "sticker", "roll_up_banner", "poster", "mounted_board"],
        default="banner",
    )
    quantity = serializers.IntegerField(min_value=1)
    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.filter(is_active=True))
    finishings = FinishingSelectionSerializer(many=True, required=False)
    hardware_finishing_rate = serializers.PrimaryKeyRelatedField(
        queryset=FinishingRate.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    size_mode = serializers.ChoiceField(choices=["standard", "custom"], required=False, default="custom")
    size_label = serializers.CharField(required=False, allow_blank=True, default="")
    input_unit = serializers.ChoiceField(choices=["mm", "cm", "m", "in"], required=False, default="mm")
    width_input = serializers.DecimalField(required=False, allow_null=True, max_digits=10, decimal_places=3, min_value=Decimal("0.001"))
    height_input = serializers.DecimalField(required=False, allow_null=True, max_digits=10, decimal_places=3, min_value=Decimal("0.001"))
    width_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    height_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def to_internal_value(self, data):
        normalized = normalize_size_payload(
            data,
            legacy_width_keys=("chosen_width_mm",),
            legacy_height_keys=("chosen_height_mm",),
        )
        return super().to_internal_value(normalized)

    def validate(self, attrs):
        attrs = validate_size_selection(attrs)
        shop = attrs["shop"]
        errors = {}
        if not attrs.get("width_mm") or not attrs.get("height_mm"):
            errors["non_field_errors"] = ["width_mm and height_mm are required for large-format previews."]
        if attrs["material"].shop_id != shop.id:
            errors["material"] = ["Material must belong to the selected shop."]

        finishing_errors = []
        for selection in attrs.get("finishings") or []:
            if selection["finishing_rate"].shop_id != shop.id:
                finishing_errors.append({"finishing_rate": ["Finishing rate must belong to the selected shop."]})
            else:
                finishing_errors.append({})
        if any(item for item in finishing_errors):
            errors["finishings"] = finishing_errors

        hardware_rate = attrs.get("hardware_finishing_rate")
        if hardware_rate and hardware_rate.shop_id != shop.id:
            errors["hardware_finishing_rate"] = ["Hardware finishing rate must belong to the selected shop."]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class QuoteDraftCreateSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, allow_blank=True)
    shop = serializers.PrimaryKeyRelatedField(queryset=Shop.objects.all(), required=False, allow_null=True)
    selected_product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), required=False, allow_null=True)
    calculator_inputs_snapshot = serializers.JSONField()
    pricing_snapshot = serializers.JSONField(required=False)
    custom_product_snapshot = serializers.JSONField(required=False)
    request_details_snapshot = serializers.JSONField(required=False)


class QuoteDraftUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, allow_blank=True)
    shop = serializers.PrimaryKeyRelatedField(queryset=Shop.objects.all(), required=False, allow_null=True)
    selected_product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), required=False, allow_null=True)
    calculator_inputs_snapshot = serializers.JSONField(required=False)
    pricing_snapshot = serializers.JSONField(required=False)
    custom_product_snapshot = serializers.JSONField(required=False)
    request_details_snapshot = serializers.JSONField(required=False)


class QuoteDraftReadSerializer(serializers.ModelSerializer):
    generated_request_ids = serializers.SerializerMethodField()
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_slug = serializers.CharField(source="shop.slug", read_only=True)
    shop_currency = serializers.CharField(source="shop.currency", read_only=True)
    raw_status = serializers.CharField(source="status", read_only=True)
    status = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = QuoteDraft
        fields = [
            "id",
            "draft_reference",
            "title",
            "status",
            "raw_status",
            "status_label",
            "shop",
            "shop_name",
            "shop_slug",
            "shop_currency",
            "selected_product",
            "calculator_inputs_snapshot",
            "pricing_snapshot",
            "custom_product_snapshot",
            "request_details_snapshot",
            "generated_request_ids",
            "created_at",
            "updated_at",
        ]

    def get_generated_request_ids(self, obj):
        return list(obj.generated_requests.values_list("id", flat=True))

    def get_status(self, obj):
        return normalize_quote_draft_status(
            obj.status,
            has_shop=bool(obj.shop_id),
            has_request_details=bool(obj.request_details_snapshot),
            has_pricing=bool(obj.pricing_snapshot),
        )

    def get_status_label(self, obj):
        return quote_draft_status_label(self.get_status(obj))


class QuoteDraftSendSerializer(serializers.Serializer):
    shops = serializers.PrimaryKeyRelatedField(queryset=Shop.objects.all(), many=True)
    request_details_snapshot = serializers.JSONField(required=False)


class QuoteRequestReadSerializer(serializers.ModelSerializer):
    source_draft_reference = serializers.CharField(source="source_draft.draft_reference", read_only=True)
    latest_response = serializers.SerializerMethodField()
    responses_count = serializers.SerializerMethodField()
    raw_status = serializers.CharField(source="status", read_only=True)
    status = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = QuoteRequest
        fields = [
            "id",
            "request_reference",
            "shop",
            "created_by",
            "status",
            "raw_status",
            "status_label",
            "customer_name",
            "customer_email",
            "customer_phone",
            "source_draft",
            "source_draft_reference",
            "request_snapshot",
            "latest_response",
            "responses_count",
            "created_at",
            "updated_at",
        ]

    def get_latest_response(self, obj):
        latest = obj.get_latest_response()
        if not latest:
            return None
        normalized_status = normalize_quote_response_status(latest.status)
        return {
            "id": latest.id,
            "quote_reference": latest.quote_reference,
            "status": normalized_status,
            "raw_status": latest.status,
            "status_label": quote_response_status_label(normalized_status),
            "total": latest.total,
            "turnaround_days": latest.turnaround_days,
            "turnaround_hours": latest.turnaround_hours,
            "estimated_ready_at": latest.estimated_ready_at,
            "human_ready_text": latest.human_ready_text,
            "turnaround_label": latest.turnaround_label,
            "response_snapshot": latest.response_snapshot,
            "revised_pricing_snapshot": latest.revised_pricing_snapshot,
            "created_at": latest.created_at,
            "sent_at": latest.sent_at,
        }

    def get_responses_count(self, obj):
        return obj.shop_quotes.count()

    def get_status(self, obj):
        return normalize_quote_request_status(obj.status)

    def get_status_label(self, obj):
        return quote_request_status_label(self.get_status(obj))


class DashboardQuoteRequestSummarySerializer(serializers.ModelSerializer):
    source_draft_reference = serializers.CharField(source="source_draft.draft_reference", read_only=True)
    latest_response = serializers.SerializerMethodField()
    raw_status = serializers.CharField(source="status", read_only=True)
    status = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = QuoteRequest
        fields = [
            "id",
            "request_reference",
            "shop",
            "status",
            "raw_status",
            "status_label",
            "customer_name",
            "customer_email",
            "customer_phone",
            "source_draft_reference",
            "request_snapshot",
            "latest_response",
            "created_at",
            "updated_at",
        ]

    def get_latest_response(self, obj):
        latest_response_id = getattr(obj, "latest_response_id", None)
        if not latest_response_id:
            return None
        raw_status = getattr(obj, "latest_response_status", "")
        normalized_status = normalize_quote_response_status(raw_status)
        return {
            "id": latest_response_id,
            "quote_reference": getattr(obj, "latest_response_reference", ""),
            "status": normalized_status,
            "raw_status": raw_status,
            "status_label": quote_response_status_label(normalized_status),
            "total": getattr(obj, "latest_response_total", None),
            "response_snapshot": getattr(obj, "latest_response_snapshot", None),
            "revised_pricing_snapshot": getattr(obj, "latest_revised_pricing_snapshot", None),
            "created_at": getattr(obj, "latest_response_created_at", None),
            "sent_at": getattr(obj, "latest_response_sent_at", None),
        }

    def get_status(self, obj):
        return normalize_quote_request_status(obj.status)

    def get_status_label(self, obj):
        return quote_request_status_label(self.get_status(obj))


class QuoteResponseCreateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["draft", "sent", "modified", "accepted", "rejected", "expired"])
    response_snapshot = serializers.JSONField()
    revised_pricing_snapshot = serializers.JSONField(required=False)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    note = serializers.CharField(required=False, allow_blank=True)
    turnaround_days = serializers.IntegerField(required=False, min_value=0)
    turnaround_hours = serializers.IntegerField(required=False, min_value=1)

    def validate_status(self, value):
        normalized = denormalize_quote_response_status(value)
        if normalized not in {
            ShopQuoteStatus.PENDING,
            ShopQuoteStatus.SENT,
            ShopQuoteStatus.MODIFIED,
            ShopQuoteStatus.ACCEPTED,
            ShopQuoteStatus.REJECTED,
            ShopQuoteStatus.EXPIRED,
        }:
            raise serializers.ValidationError("Unsupported quote response status.")
        return normalized


class QuoteResponseUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["draft", "sent", "modified", "accepted", "rejected", "expired"])
    response_snapshot = serializers.JSONField(required=False)
    revised_pricing_snapshot = serializers.JSONField(required=False)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    note = serializers.CharField(required=False, allow_blank=True)
    turnaround_days = serializers.IntegerField(required=False, min_value=0)
    turnaround_hours = serializers.IntegerField(required=False, min_value=1)

    def validate_status(self, value):
        normalized = denormalize_quote_response_status(value)
        if normalized not in {
            ShopQuoteStatus.PENDING,
            ShopQuoteStatus.SENT,
            ShopQuoteStatus.MODIFIED,
            ShopQuoteStatus.ACCEPTED,
            ShopQuoteStatus.REJECTED,
            ShopQuoteStatus.EXPIRED,
        }:
            raise serializers.ValidationError("Unsupported quote response status.")
        return normalized


class QuoteResponseReadSerializer(serializers.ModelSerializer):
    request_reference = serializers.CharField(source="quote_request.request_reference", read_only=True)
    raw_status = serializers.CharField(source="status", read_only=True)
    status = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()
    whatsapp_available = serializers.SerializerMethodField()
    whatsapp_url = serializers.SerializerMethodField()
    whatsapp_label = serializers.SerializerMethodField()

    class Meta:
        model = ShopQuote
        fields = [
            "id",
            "quote_reference",
            "quote_request",
            "request_reference",
            "shop",
            "status",
            "raw_status",
            "status_label",
            "total",
            "note",
            "turnaround_days",
            "turnaround_hours",
            "estimated_ready_at",
            "human_ready_text",
            "turnaround_label",
            "response_snapshot",
            "revised_pricing_snapshot",
            "revision_number",
            "pricing_locked_at",
            "created_at",
            "sent_at",
            "whatsapp_available",
            "whatsapp_url",
            "whatsapp_label",
        ]

    def get_status(self, obj):
        return normalize_quote_response_status(obj.status)

    def get_status_label(self, obj):
        return quote_response_status_label(self.get_status(obj))

    def _whatsapp_handoff(self, obj):
        request = self.context.get("request")
        viewer_role = "buyer"
        if request and getattr(request.user, "id", None) == obj.shop.owner_id:
            viewer_role = "shop"
        return build_quote_request_whatsapp_handoff(obj.quote_request, viewer_role=viewer_role)

    def get_whatsapp_available(self, obj):
        return self._whatsapp_handoff(obj).get("available", False)

    def get_whatsapp_url(self, obj):
        return self._whatsapp_handoff(obj).get("url", "")

    def get_whatsapp_label(self, obj):
        return self._whatsapp_handoff(obj).get("label", "")
