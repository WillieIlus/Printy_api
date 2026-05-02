from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel
from shops.models import Shop

from .choices import MachineType, PaperCategory, PaperType, SHEET_SIZE_DIMENSIONS, SheetSize


# ---------------------------------------------------------------------------
# Size models — for imposition and pricing
# ---------------------------------------------------------------------------

SIZE_TYPE_PRODUCTION = "PRODUCTION"
SIZE_TYPE_FINAL = "FINAL"
SIZE_TYPE_CHOICES = [
    (SIZE_TYPE_PRODUCTION, _("Production (parent/press sheet)")),
    (SIZE_TYPE_FINAL, _("Final (finished size)")),
]


class BaseSize(models.Model):
    """Abstract base for dimensional sizes (width × height in mm)."""

    name = models.CharField(
        max_length=100,
        verbose_name=_("name"),
        help_text=_("Display name (e.g. SRA3, A4, Business Card)."),
    )
    width_mm = models.PositiveIntegerField(
        verbose_name=_("width (mm)"),
        help_text=_("Width in millimeters."),
    )
    height_mm = models.PositiveIntegerField(
        verbose_name=_("height (mm)"),
        help_text=_("Height in millimeters."),
    )
    size_type = models.CharField(
        max_length=20,
        choices=SIZE_TYPE_CHOICES,
        verbose_name=_("size type"),
        help_text=_("PRODUCTION = parent/press sheet; FINAL = finished size."),
    )

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.name} ({self.width_mm}×{self.height_mm}mm)"


class ProductionPaperSize(BaseSize, TimeStampedModel):
    """
    Parent/press sheet sizes: SRA3, B2, 13×19, etc.
    Used for imposition: how many finished pieces fit on one sheet.
    """

    size_type = models.CharField(
        max_length=20,
        choices=SIZE_TYPE_CHOICES,
        default=SIZE_TYPE_PRODUCTION,
        verbose_name=_("size type"),
        editable=False,
    )
    code = models.CharField(
        max_length=30,
        unique=True,
        verbose_name=_("code"),
        help_text=_("Short code for matching (e.g. SRA3, A3, B2). Used by PrintingRate."),
    )

    class Meta:
        verbose_name = _("production paper size")
        verbose_name_plural = _("production paper sizes")
        ordering = ["width_mm", "height_mm"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class FinalPaperSize(BaseSize, TimeStampedModel):
    """
    Finished sizes: A4, A5, business card (90×55mm), etc.
    Used for product defaults and imposition item dimensions.
    """

    size_type = models.CharField(
        max_length=20,
        choices=SIZE_TYPE_CHOICES,
        default=SIZE_TYPE_FINAL,
        verbose_name=_("size type"),
        editable=False,
    )

    class Meta:
        verbose_name = _("final paper size")
        verbose_name_plural = _("final paper sizes")
        ordering = ["width_mm", "height_mm"]


# ---------------------------------------------------------------------------
# Machine, Paper, etc.
# ---------------------------------------------------------------------------

class Machine(TimeStampedModel):
    """Printing machine belonging to a shop."""

    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="machines",
        verbose_name=_("shop"),
        help_text=_("Shop that owns this machine."),
    )
    name = models.CharField(
        max_length=255,
        default="",
        verbose_name=_("name"),
        help_text=_("Display name of the machine."),
    )
    machine_type = models.CharField(
        max_length=20,
        choices=MachineType.choices,
        default=MachineType.DIGITAL,
        verbose_name=_("machine type"),
        help_text=_("Type of printing machine (offset, digital, large format)."),
    )
    max_width_mm = models.PositiveIntegerField(
        verbose_name=_("max width (mm)"),
        help_text=_("Maximum printable width in millimeters."),
    )
    max_height_mm = models.PositiveIntegerField(
        verbose_name=_("max height (mm)"),
        help_text=_("Maximum printable height in millimeters."),
    )
    min_gsm = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("min GSM"),
        help_text=_("Minimum paper weight (grams per square metre) supported."),
    )
    max_gsm = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("max GSM"),
        help_text=_("Maximum paper weight (grams per square metre) supported."),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("is active"),
        help_text=_("Whether the machine is active and available."),
    )

    class Meta:
        ordering = ["shop", "name"]
        verbose_name = _("machine")
        verbose_name_plural = _("machines")

    def __str__(self):
        return f"{self.name} ({self.shop.name})"


class Paper(TimeStampedModel):
    """
    Paper stock for SHEET printing (pre-cut sheets: A4, A3, SRA3, etc.).
    Tracks physical inventory (quantity_in_stock), buying/selling price per sheet.
    Not redundant with Material: Paper = sheet-fed; Material = large-format by area.
    """

    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="papers",
        verbose_name=_("shop"),
        help_text=_("Shop that owns this paper stock."),
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("name"),
        help_text=_("Optional name for the paper (e.g. Art 130)."),
    )
    production_size = models.ForeignKey(
        ProductionPaperSize,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="papers",
        verbose_name=_("production size"),
        help_text=_("Parent sheet size for imposition. When set, width/height come from here."),
    )
    sheet_size = models.CharField(
        max_length=20,
        choices=SheetSize.choices,
        default=SheetSize.A4,
        verbose_name=_("sheet size"),
        help_text=_("Standard sheet size (A4, A3, SRA3, etc.). Kept for PrintingRate matching."),
    )
    gsm = models.PositiveIntegerField(
        verbose_name=_("GSM"),
        help_text=_("Paper weight in grams per square metre."),
    )
    paper_type = models.CharField(
        max_length=20,
        choices=PaperType.choices,
        default=PaperType.UNCOATED,
        verbose_name=_("paper type"),
        help_text=_("Type of paper (coated, uncoated, gloss, etc.)."),
    )
    category = models.CharField(
        max_length=30,
        choices=PaperCategory.choices,
        default=PaperCategory.OTHER,
        verbose_name=_("paper category"),
        help_text=_("Marketplace-facing category such as matt, tictac, conqueror, bond, or cover board."),
    )
    display_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("display name"),
        help_text=_("Human-ready stock label shown in calculator dropdowns (e.g. Matt 130gsm, Conqueror 120gsm)."),
    )
    is_cover_stock = models.BooleanField(
        default=False,
        verbose_name=_("is cover stock"),
        help_text=_("Whether this stock is suitable for booklet covers or heavier cover applications."),
    )
    is_insert_stock = models.BooleanField(
        default=False,
        verbose_name=_("is insert stock"),
        help_text=_("Whether this stock is suitable for booklet inserts or inner pages."),
    )
    is_sticker_stock = models.BooleanField(
        default=False,
        verbose_name=_("is sticker stock"),
        help_text=_("Whether this stock is suitable for label stickers / tictac work."),
    )
    is_specialty = models.BooleanField(
        default=False,
        verbose_name=_("is specialty"),
        help_text=_("Marks specialty papers such as conqueror, ivory, kraft, or custom branded stocks."),
    )
    width_mm = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("width (mm)"),
        help_text=_("Sheet width in millimeters (auto-filled from sheet size)."),
    )
    height_mm = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("height (mm)"),
        help_text=_("Sheet height in millimeters (auto-filled from sheet size)."),
    )
    buying_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_("buying price"),
        help_text=_("Cost price per sheet."),
    )
    selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_("selling price"),
        help_text=_("Selling price per sheet."),
    )
    quantity_in_stock = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("quantity in stock"),
        help_text=_("Number of sheets currently in stock."),
    )
    reorder_level = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("reorder level"),
        help_text=_("Stock level that triggers reorder alert."),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("is active"),
        help_text=_("Whether this paper stock is active."),
    )
    is_default = models.BooleanField(
        default=False,
        verbose_name=_("is default"),
        help_text=_(
            "Use this paper for imposition/pricing when no paper is specified. "
            "Selection order: default paper > most economical (lowest cost per sheet) > only available."
        ),
    )

    class Meta:
        ordering = ["shop", "sheet_size", "gsm", "paper_type"]
        verbose_name = _("paper")
        verbose_name_plural = _("papers")
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "sheet_size", "gsm", "paper_type"],
                name="unique_shop_sheet_gsm_paper",
            ),
            models.UniqueConstraint(
                fields=["shop"],
                condition=models.Q(is_default=True),
                name="unique_shop_default_paper",
            ),
        ]

    def __str__(self):
        return self.marketplace_label

    def get_dimensions_mm(self) -> tuple[int | None, int | None]:
        """Return (width_mm, height_mm) for imposition. Prefers production_size when set."""
        if self.production_size_id:
            return self.production_size.width_mm, self.production_size.height_mm
        return self.width_mm, self.height_mm

    @property
    def category_label(self) -> str:
        return self.get_category_display() or self.category or self.get_paper_type_display()

    @property
    def marketplace_label(self) -> str:
        base = (self.display_name or self.name or "").strip()
        if base:
            return base
        return f"{self.category_label} {self.gsm}gsm"

    def supports_usage(self, usage: str) -> bool:
        usage = (usage or "").strip().lower()
        if usage == "cover":
            return bool(self.is_cover_stock or self.gsm >= 170 or self.category in {PaperCategory.ARTCARD, PaperCategory.COVER_BOARD})
        if usage == "insert":
            return bool(self.is_insert_stock or not self.is_sticker_stock)
        if usage == "sticker":
            return bool(self.is_sticker_stock or self.category == PaperCategory.TICTAC)
        return True

    def save(self, *args, **kwargs):
        # When setting is_default=True, clear it on other papers in the same shop
        if self.is_default and self.shop_id:
            Paper.objects.filter(shop_id=self.shop_id).exclude(pk=self.pk).update(is_default=False)
        # Keep the persisted label aligned with the editable name/category/gsm inputs.
        fallback_name = (self.name or "").strip()
        self.name = fallback_name
        self.display_name = fallback_name or f"{self.get_category_display() or self.get_paper_type_display()} {self.gsm}gsm"
        # Prefer production_size for dimensions; fallback to sheet_size lookup
        if self.production_size_id:
            if self.width_mm is None:
                self.width_mm = self.production_size.width_mm
            if self.height_mm is None:
                self.height_mm = self.production_size.height_mm
            if not self.sheet_size or self.sheet_size == SheetSize.CUSTOM:
                self.sheet_size = self.production_size.code
        elif self.sheet_size in SHEET_SIZE_DIMENSIONS and (
            self.width_mm is None or self.height_mm is None
        ):
            w, h = SHEET_SIZE_DIMENSIONS[self.sheet_size]
            if self.width_mm is None:
                self.width_mm = w
            if self.height_mm is None:
                self.height_mm = h
        super().save(*args, **kwargs)
