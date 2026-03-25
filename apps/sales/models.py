from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import BaseRetailModel, TimeStampedModel


class RegisterSession(BaseRetailModel):
    """
    CRITICAL FOR AUDITS: Tracks a Cashier's Shift.
    You cannot run a secure shop without knowing how much cash
    started in the drawer and how much ended in the drawer.
    """

    class Status(models.TextChoices):
        OPEN = 'OPEN', _('Open')
        CLOSED = 'CLOSED', _('Closed')
        DISCREPANCY = 'DISCREPANCY', _('Closed with Discrepancy')

    location = models.ForeignKey('location.Location', on_delete=models.PROTECT, related_name='register_sessions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='sessions')

    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cash amount in drawer at start")
    closing_balance_expected = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                                   help_text="System calculated expected cash")
    closing_balance_actual = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                                 help_text="Actual cash counted by cashier")

    # Financial Summary of the Shift
    total_cash_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_momo_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_card_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    notes = models.TextField(blank=True, help_text="Explanation for discrepancies")

    def __str__(self):
        return f"Session: {self.user.username} @ {self.location.name} ({self.start_time.date()})"

    @property
    def discrepancy(self):
        if self.closing_balance_actual is not None:
            return self.closing_balance_actual - self.closing_balance_expected
        return 0.00


class Sale(BaseRetailModel):
    """
    Represents a Customer Transaction (Receipt/Invoice).
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = 'PENDING', _('Pending Payment / Draft')
        COMPLETED = 'COMPLETED', _('Completed')
        CANCELLED = 'CANCELLED', _('Cancelled')
        REFUNDED = 'REFUNDED', _('Fully Refunded')
        PARTIAL_REFUND = 'PARTIAL', _('Partially Refunded')
        ON_HOLD = 'HOLD', _('On Hold / Parked')

    class OrderType(models.TextChoices):
        WALK_IN = 'WALK_IN', _('Walk-In')
        DELIVERY = 'DELIVERY', _('Delivery')
        PICKUP = 'PICKUP', _('Store Pickup')
        ONLINE = 'ONLINE', _('Online Order')

    class FulfillmentStatus(models.TextChoices):
        FULFILLED = 'FULFILLED', _('Items Handed Over')
        UNFULFILLED = 'UNFULFILLED', _('Pending Delivery/Pickup')
        PARTIAL = 'PARTIAL', _('Partially Fulfilled')

    # Identifiers
    invoice_number = models.CharField(max_length=50, unique=True, editable=False, db_index=True)

    # Context
    location = models.ForeignKey('location.Location', on_delete=models.PROTECT, related_name='sales')

    # The "Audit Link" - Which drawer session did this money go into?
    register_session = models.ForeignKey(RegisterSession, on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='sales')

    # Roles
    # Cashier is nullable because a salesperson might create a DRAFT sale before a cashier touches it.
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='processed_sales',
                                null=True, blank=True)
    salesperson = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='assisted_sales')
    customer = models.ForeignKey('customers.Customer', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='purchases')

    # ---------------------------------------------------------
    # FINANCIAL SUMMARY (Calculated Fields)
    # ---------------------------------------------------------
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text="Sum of items before tax")
    total_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text="Final Bill Amount")

    # Payment Tracking
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                      help_text="Sum of all SalePayments")
    change_due = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Context Flags
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT)
    order_type = models.CharField(max_length=20, choices=OrderType.choices, default=OrderType.WALK_IN)
    fulfillment_status = models.CharField(max_length=20, choices=FulfillmentStatus.choices,
                                          default=FulfillmentStatus.FULFILLED)

    notes = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            import uuid, time
            self.invoice_number = f"INV-{int(time.time())}-{uuid.uuid4().hex[:4].upper()}"
        super().save(*args, **kwargs)

    @property
    def is_fully_paid(self):
        return self.amount_paid >= self.total_amount

    @property
    def balance_remaining(self):
        return max(self.total_amount - self.amount_paid, 0)

    def __str__(self):
        return f"{self.invoice_number} - {self.total_amount}"


class SaleItem(models.Model):
    """
    Individual Line Items on the Receipt.
    """
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('products.Product', on_delete=models.PROTECT)
    source_batch = models.ForeignKey('inventory.StockBatch', on_delete=models.PROTECT, null=True, blank=True)

    quantity = models.PositiveIntegerField(default=1)

    # Locking price at moment of sale
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)

    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)

    is_refunded = models.BooleanField(default=False)

    # Special instructions (e.g., "Gift Wrap", "No Onions", "Display Unit")
    note = models.CharField(max_length=255, blank=True)

    def save(self, *args, **kwargs):
        if not self.total_price:
            self.total_price = (self.unit_price * self.quantity) - self.discount_amount
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity}x {self.product.name}"


class SaleTax(models.Model):
    """
    SNAPSHOT of taxes applied to this specific sale.
    """
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='taxes')
    tax_name = models.CharField(max_length=100)  # e.g., "VAT"
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3)  # e.g., 12.5
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.tax_name} ({self.tax_amount})"


class SalePayment(TimeStampedModel):
    """
    Handling Split Payments.
    """

    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', _('Cash')
        MOMO = 'MOMO', _('Mobile Money')
        CARD = 'CARD', _('Card')
        BANK_TRANSFER = 'BANK', _('Bank Transfer')
        CHEQUE = 'CHEQUE', _('Cheque')
        CREDIT = 'CREDIT', _('Store Credit / On Account')

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)

    # Proof of Payment
    reference_id = models.CharField(max_length=100, blank=True,
                                    help_text="Transaction ID, Cheque Number, or Receipt Ref")
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.payment_method}: {self.amount} for {self.sale.invoice_number}"


class Delivery(TimeStampedModel):
    """
    Tracks the logistics/fulfillment of a Sale.
    Supports both 3rd party providers (Yango/Uber) and In-House riders.
    """

    class Provider(models.TextChoices):
        YANGO = 'YANGO', _('Yango')
        UBER = 'UBER', _('Uber')
        BOLT = 'BOLT', _('Bolt')
        IN_HOUSE = 'IN_HOUSE', _('In-House Rider')
        DHL = 'DHL', _('DHL / FedEx')
        OTHER = 'OTHER', _('Other')

    class Status(models.TextChoices):
        PENDING = 'PENDING', _('Pending Pickup')
        DISPATCHED = 'DISPATCHED', _('Dispatched / In Transit')
        DELIVERED = 'DELIVERED', _('Delivered')
        FAILED = 'FAILED', _('Failed / Returned')

    # Link one-to-one with Sale. A sale usually has one delivery.
    sale = models.OneToOneField(Sale, on_delete=models.CASCADE, related_name='delivery_details')

    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.IN_HOUSE)

    # Rider / Driver Details (Captured for verification)
    rider_name = models.CharField(max_length=255, blank=True, help_text="Name of the rider picking up")
    rider_phone = models.CharField(max_length=20, blank=True, help_text="Phone number for the rider")
    vehicle_details = models.CharField(max_length=255, blank=True, help_text="License Plate / Bike Model / Color")

    # Tracking
    tracking_reference = models.CharField(max_length=255, blank=True, help_text="Yango Link, Tracking ID, or Ride Ref")

    # Location
    destination_address = models.TextField(help_text="Full delivery address provided by customer")
    google_maps_link = models.URLField(blank=True, help_text="Pinned location link")

    # Financials (Logistics Profit/Loss)
    delivery_fee_charged = models.DecimalField(max_digits=10, decimal_places=2, default=0.00,
                                               help_text="Amount charged to customer on receipt")
    cost_to_business = models.DecimalField(max_digits=10, decimal_places=2, default=0.00,
                                           help_text="Actual cost paid to Yango/Rider")

    # Proof of Execution (Screenshots/Photos)
    proof_of_pickup = models.ImageField(upload_to='deliveries/pickup/', null=True, blank=True,
                                        help_text="Photo of rider with items or app screenshot")
    proof_of_delivery = models.ImageField(upload_to='deliveries/dropoff/', null=True, blank=True,
                                          help_text="Photo of item at destination")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True, help_text="Driver instructions or issues")

    def __str__(self):
        return f"{self.provider} Delivery for {self.sale.invoice_number}"








