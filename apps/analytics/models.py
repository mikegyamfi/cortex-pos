from django.db import models
from apps.core.models import TimeStampedModel


class DailyShopSummary(TimeStampedModel):
    """
    Performance Snapshots.
    Updated to include Expenses and Targets.
    """
    date = models.DateField(db_index=True)
    location = models.ForeignKey('location.Location', on_delete=models.CASCADE, related_name='daily_summaries')

    # The Big Numbers (Revenue)
    total_revenue = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_profit = models.DecimalField(max_digits=15, decimal_places=2, default=0.00,
                                       help_text="Gross Profit (Sales - Cost of Goods)")

    # Expenses & Net Profit
    total_expenses = models.DecimalField(max_digits=15, decimal_places=2, default=0.00,
                                         help_text="Sum of approved expenses for the day")
    net_profit = models.DecimalField(max_digits=15, decimal_places=2, default=0.00,
                                     help_text="Total Profit - Total Expenses")

    # Taxes & Discounts
    total_tax_collected = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_discount_given = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Targets (Historical Record)
    target_for_the_day = models.DecimalField(max_digits=15, decimal_places=2, default=0.00,
                                             help_text="What was the target set for this specific date?")
    target_achievement_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)

    # Operational Metrics
    transaction_count = models.PositiveIntegerField(default=0)
    items_sold_count = models.PositiveIntegerField(default=0)
    average_basket_value = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Payment Breakdown
    cash_payments = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    momo_payments = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    card_payments = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)

    class Meta:
        unique_together = ('date', 'location')
        ordering = ['-date']
        verbose_name_plural = "Daily Shop Summaries"

    def __str__(self):
        return f"Summary: {self.location.name} on {self.date}"


class SlowMovingStock(TimeStampedModel):
    """
    Identifies 'Dead Stock' - Items that haven't sold in X days.
    """
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE)
    location = models.ForeignKey('location.Location', on_delete=models.CASCADE)

    last_sold_date = models.DateField()
    days_since_last_sale = models.PositiveIntegerField()
    current_stock_quantity = models.IntegerField()

    estimated_tied_capital = models.DecimalField(max_digits=15, decimal_places=2)

    def __str__(self):
        return f"Dead Stock: {self.product.name} ({self.days_since_last_sale} days)"