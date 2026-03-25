from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps import location
from apps.core.models import TimeStampedModel


class User(AbstractUser):
    """
    Main Authentication model for the Retail System.
    """

    class Role(models.TextChoices):
        OWNER = 'OWNER', _('Owner')
        MANAGER = 'MANAGER', _('Manager')
        WAREHOUSE_STAFF = 'WAREHOUSE_STAFF', _('Warehouse Staff')
        SALESPERSON = 'SALESPERSON', _('Salesperson')
        CASHIER = 'CASHIER', _('Cashier')
        ACCOUNTANT = 'ACCOUNTANT', _('Accountant')

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.SALESPERSON
    )
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    assigned_location = models.ForeignKey(
        'location.Location',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff'
    )

    # Track which location the user is currently working at (for roaming staff)
    current_session_location = models.ForeignKey(
        'location.Location',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_users'
    )
    requires_password_change = models.BooleanField(
        default=False,
        help_text="If True, user must change password on next login."
    )

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER


class UserProfile(TimeStampedModel):
    """
    Stores additional personal/HR information for workers.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    date_of_birth = models.DateField(null=True, blank=True)
    home_address = models.TextField(blank=True)

    # ID Verification
    id_type = models.CharField(max_length=50, blank=True)  # e.g., Ghana Card, Passport
    id_number = models.CharField(max_length=100, blank=True)
    id_document = models.FileField(upload_to='staff/ids/', null=True, blank=True)

    # Emergency Contact
    emergency_contact_name = models.CharField(max_length=255, blank=True)
    emergency_contact_phone = models.CharField(max_length=15, blank=True)
    emergency_contact_relation = models.CharField(max_length=100, blank=True)

    # Employment Details
    date_joined = models.DateField(null=True, blank=True)
    base_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    contract_file = models.FileField(upload_to='staff/contracts/', null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Internal HR notes about the worker")

    def __str__(self):
        return f"Profile for {self.user.username}"