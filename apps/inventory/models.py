from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from apps.core.models import BaseRetailModel, TimeStampedModel


class Supplier(BaseRetailModel):
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    tax_id = models.CharField(max_length=100, blank=True, help_text="TIN / VAT Number")
    credit_period_days = models.PositiveIntegerField(default=0, help_text="Payment terms in days")

    def __str__(self):
        return self.name


class StockBatch(TimeStampedModel):
    """
    The CORE of the inventory system.
    Tracks a specific batch of products at a specific location.
    Essential for Expiry (FEFO) and Cost tracking.
    """
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE, related_name='batches')
    location = models.ForeignKey('location.Location', on_delete=models.CASCADE, related_name='stock_batches')
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)

    batch_number = models.CharField(max_length=100, blank=True, help_text="Manufacturer Batch/Lot Number")
    quantity = models.IntegerField(default=0)

    # Financials per batch
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cost per unit for this specific batch")

    # Dates
    manufactured_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    received_date = models.DateTimeField(default=timezone.now)

    class Meta:
        # Unique constraint to prevent duplicate batches if needed,
        # but often we just want to aggregate.
        # Indexing for FEFO is crucial.
        ordering = ['expiry_date', 'received_date']
        indexes = [
            models.Index(fields=['product', 'location', 'expiry_date']),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.batch_number or 'No Batch'} ({self.quantity}) @ {self.location.name}"

    @property
    def is_expired(self):
        if self.expiry_date:
            return self.expiry_date < timezone.now().date()
        return False

    @property
    def days_to_expiry(self):
        if self.expiry_date:
            delta = self.expiry_date - timezone.now().date()
            return delta.days
        return None


class StockTransfer(BaseRetailModel):
    """
    Manages movement between Warehouses and Shops.
    """

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft')
        PENDING_APPROVAL = 'PENDING', _('Pending Approval')
        APPROVED = 'APPROVED', _('Approved')  # Ready to be picked
        IN_TRANSIT = 'TRANSIT', _('In Transit')
        RECEIVED = 'RECEIVED', _('Received')
        CANCELLED = 'CANCELLED', _('Cancelled')
        REJECTED = 'REJECTED', _('Rejected')

    reference_number = models.CharField(max_length=50, unique=True, editable=False)

    source_location = models.ForeignKey(
        'location.Location',
        on_delete=models.PROTECT,
        related_name='outgoing_transfers'
    )
    destination_location = models.ForeignKey(
        'location.Location',
        on_delete=models.PROTECT,
        related_name='incoming_transfers'
    )

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # Logistics
    driver_name = models.CharField(max_length=255, blank=True)
    vehicle_number = models.CharField(max_length=50, blank=True)
    estimated_arrival = models.DateTimeField(null=True, blank=True)

    # Audit
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     related_name='transfer_requests')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    related_name='transfer_approvals')
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    related_name='transfer_receipts')

    notes = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.reference_number:
            # Simple ID generation logic; in prod use a more robust sequence
            import uuid
            self.reference_number = f"TRF-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference_number}: {self.source_location} -> {self.destination_location}"


class StockTransferItem(models.Model):
    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE)

    # We request a quantity, but fulfill from specific batches
    quantity_requested = models.PositiveIntegerField()
    quantity_sent = models.PositiveIntegerField(default=0)
    quantity_received = models.PositiveIntegerField(default=0)

    # If items are damaged in transit
    quantity_damaged = models.PositiveIntegerField(default=0)
    damage_notes = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.product.name} ({self.quantity_requested}) in {self.transfer.reference_number}"


class StockAdjustment(TimeStampedModel):
    """
    For manual corrections (Damages, Theft, Found Stock, Expired Disposal).
    """

    class Reason(models.TextChoices):
        DAMAGE = 'DAMAGE', _('Damaged')
        THEFT = 'THEFT', _('Theft')
        EXPIRED = 'EXPIRED', _('Expired')
        COUNT_CORRECTION = 'CORRECTION', _('Inventory Count Correction')
        OTHER = 'OTHER', _('Other')

    location = models.ForeignKey('location.Location', on_delete=models.PROTECT)
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT)
    adjusted_quantity = models.IntegerField(help_text="Negative to remove, Positive to add")
    reason = models.CharField(max_length=20, choices=Reason.choices)
    notes = models.TextField()
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.reason} - {self.batch.product.name} ({self.adjusted_quantity})"