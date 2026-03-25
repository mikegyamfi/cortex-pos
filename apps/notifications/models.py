from django.db import models
from apps.core.models import TimeStampedModel


class NotificationTemplate(TimeStampedModel):
    """
    Pre-written messages for consistency.
    e.g., 'Welcome SMS', 'Receipt SMS', 'Holiday Promo'
    """

    class Type(models.TextChoices):
        SMS = 'SMS', 'SMS'
        EMAIL = 'EMAIL', 'Email'

    name = models.CharField(max_length=100)  # e.g., "Purchase Receipt"
    type = models.CharField(max_length=10, choices=Type.choices, default=Type.SMS)

    # The message content. Can use placeholders like {{ name }}, {{ amount }}
    content = models.TextField(help_text="Use {{ name }} for customer name, {{ amount }} for transaction total.")

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.type})"


class NotificationLog(TimeStampedModel):
    """
    Audit Trail: Tracks every single message sent.
    Useful for checking 'Did the customer actually get the receipt?'
    """

    class Status(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        SENT = 'SENT', 'Sent'
        FAILED = 'FAILED', 'Failed'

    customer = models.ForeignKey('customers.Customer', on_delete=models.CASCADE, related_name='notifications')

    # If linked to a specific sale (for receipts)
    sale = models.ForeignKey('sales.Sale', on_delete=models.SET_NULL, null=True, blank=True)

    type = models.CharField(max_length=10, choices=NotificationTemplate.Type.choices)
    recipient = models.CharField(max_length=255, help_text="Phone number or Email address used")
    message_content = models.TextField()

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)

    # Gateway Response (e.g., Message ID from Arkesel/Hubtel)
    gateway_response = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)

    def __str__(self):
        return f"{self.type} to {self.recipient}: {self.status}"






