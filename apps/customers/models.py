from django.db import models
from django.conf import settings
from apps.core.models import BaseRetailModel, TimeStampedModel


class Customer(BaseRetailModel):
    """
    The 'Backlog' of all people who have shopped with us.
    Designed for 'Silent Accumulation' - we can have a profile
    with just a phone number and nothing else.
    """
    # Essential for SMS Receipts
    phone_number = models.CharField(max_length=20, unique=True, db_index=True)

    # Optional Details (captured only if they want to give it)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(unique=True, null=True, blank=True)
    address = models.TextField(blank=True)

    # Marketing Flags (GDPR/Data Protection compliance)
    accepts_marketing_sms = models.BooleanField(default=True)
    accepts_marketing_email = models.BooleanField(default=False)

    # Auto-Calculated Segmentation (Updated via Signals on every sale)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_visits = models.PositiveIntegerField(default=0)
    last_visit_date = models.DateTimeField(null=True, blank=True)

    # Store Credit / Wallet
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    class Meta:
        ordering = ['-last_visit_date']

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.phone_number})".strip()

    @property
    def get_display_name(self):
        if self.first_name:
            return f"{self.first_name} {self.last_name}"
        return "Valued Customer"


class CustomerGroup(TimeStampedModel):
    """
    For Bulk SMS Segmentation.
    e.g., "High Spenders", "Haven't visited in 30 days", "Wholesalers"
    """
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    customers = models.ManyToManyField(Customer, related_name='groups', blank=True)

    def __str__(self):
        return self.name






