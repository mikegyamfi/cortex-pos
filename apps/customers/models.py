from django.db import models
from django.conf import settings
from apps.core.models import BaseRetailModel, TimeStampedModel


class Customer(BaseRetailModel):
    """
    The 'Backlog' of all people who have shopped with us.
    Designed for 'Silent Accumulation' - we can have a profile
    with just a phone number and nothing else.
    """
    # Which shop this customer belongs to. Each shop keeps its own customer list.
    location = models.ForeignKey(
        'location.Location', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='customers'
    )

    # Essential for SMS Receipts. Unique PER SHOP (not globally) — the same
    # person can be a customer of two different shops.
    phone_number = models.CharField(max_length=20, db_index=True)

    # Optional Details (captured only if they want to give it)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(null=True, blank=True)
    address = models.TextField(blank=True)

    # A distributor is a customer who buys on the distributor price list. They
    # get their own page, but stay Customers so debt, arrears, statements and
    # SMS all keep working the same way.
    is_distributor = models.BooleanField(
        default=False, db_index=True,
        help_text="Buys at the distributor price list and appears on the Distributors page."
    )
    company_name = models.CharField(max_length=255, blank=True,
                                    help_text="Trading name, for distributors and businesses.")
    backup_phone = models.CharField(max_length=20, blank=True,
                                    help_text="Second number to reach them on.")
    contact_person = models.CharField(max_length=150, blank=True,
                                      help_text="Who to speak to at the company.")
    notes = models.TextField(blank=True)

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
        constraints = [
            models.UniqueConstraint(
                fields=['location', 'phone_number'], name='uniq_customer_phone_per_location'
            ),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.phone_number})".strip()

    @property
    def get_display_name(self):
        if self.company_name:
            return self.company_name
        if self.first_name:
            return f"{self.first_name} {self.last_name}".strip()
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






