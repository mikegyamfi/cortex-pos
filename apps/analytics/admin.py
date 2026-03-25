from django.contrib import admin
from .models import DailyShopSummary, SlowMovingStock


@admin.register(DailyShopSummary)
class DailyShopSummaryAdmin(admin.ModelAdmin):
    list_display = ('date', 'location', 'total_revenue', 'total_profit', 'transaction_count')
    list_filter = ('location', 'date')
    date_hierarchy = 'date'

    # Prevent manual editing of historical data to preserve audit integrity
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SlowMovingStock)
class SlowMovingStockAdmin(admin.ModelAdmin):
    list_display = ('product', 'location', 'days_since_last_sale', 'current_stock_quantity')
    list_filter = ('location',)