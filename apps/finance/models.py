from django.conf import settings
from django.db import models
from apps.core.models import BaseRetailModel, TimeStampedModel
from django.utils.translation import gettext_lazy as _



class Tax(BaseRetailModel):
    """
    Global Tax Configuration.
    Define all your taxes here (VAT, NHIL, COVID Levy, City Tax).
    """

    class TaxType(models.TextChoices):
        PERCENTAGE = 'PERCENTAGE', 'Percentage (%)'
        FIXED = 'FIXED', 'Fixed Amount'

    name = models.CharField(max_length=100)  # e.g., "VAT"
    rate = models.DecimalField(max_digits=6, decimal_places=3, help_text="e.g., 12.5 for 12.5%")
    tax_type = models.CharField(max_length=20, choices=TaxType.choices, default=TaxType.PERCENTAGE)

    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    # Order of application if you have compound taxes (advanced usage)
    priority = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.name} ({self.rate}{'%' if self.tax_type == 'PERCENTAGE' else ''})"


class ExpenseCategory(BaseRetailModel):
    """
    Grouping expenses: 'Utilities', 'Staff Welfare', 'Transport', 'Maintenance'.
    """
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Expense Categories"

    def __str__(self):
        return self.name


class Expense(TimeStampedModel):
    """
    Tracks money leaving the shop (Petty Cash or Bank Spend).
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', _('Pending Approval')
        APPROVED = 'APPROVED', _('Approved')
        REJECTED = 'REJECTED', _('Rejected')

    location = models.ForeignKey('location.Location', on_delete=models.PROTECT, related_name='expenses')
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(help_text="What was bought? e.g., 'Fuel for Generator'")

    # Audit Trail
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     related_name='requested_expenses')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='approved_expenses')

    # Proof
    receipt_image = models.ImageField(upload_to='finance/receipts/', null=True, blank=True,
                                      help_text="Photo of the receipt")

    # Context
    date_incurred = models.DateField(db_index=True)
    is_paid_from_till = models.BooleanField(default=True, help_text="Was this taken from the Cashier's active drawer?")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    def __str__(self):
        return f"{self.category.name}: {self.amount} ({self.location.name})"


class RevenueTarget(TimeStampedModel):
    """
    Gamification & Motivation.
    Sets goals for specific locations to drive performance.
    """

    class Period(models.TextChoices):
        DAILY = 'DAILY', _('Daily Target')
        WEEKLY = 'WEEKLY', _('Weekly Target')
        MONTHLY = 'MONTHLY', _('Monthly Target')

    location = models.ForeignKey('location.Location', on_delete=models.CASCADE, related_name='targets')
    period = models.CharField(max_length=20, choices=Period.choices, default=Period.MONTHLY)

    target_amount = models.DecimalField(max_digits=15, decimal_places=2)

    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Leave blank if this target is indefinite")

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.period} Target for {self.location.name}: {self.target_amount}"















