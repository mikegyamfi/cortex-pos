from django.contrib import admin
from .models import Supplier, StockBatch, StockTransfer, StockTransferItem, StockAdjustment


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_person', 'phone', 'email')
    search_fields = ('name', 'contact_person')


@admin.register(StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = ('product', 'location', 'quantity', 'expiry_date', 'batch_number', 'cost_price')
    list_filter = ('location', 'expiry_date', 'supplier')
    search_fields = ('product__name', 'batch_number')
    date_hierarchy = 'received_date'


class StockTransferItemInline(admin.TabularInline):
    model = StockTransferItem
    extra = 0


@admin.register(StockTransfer)
class StockTransferAdmin(admin.ModelAdmin):
    inlines = [StockTransferItemInline]
    list_display = ('reference_number', 'source_location', 'destination_location', 'status', 'created_at')
    list_filter = ('status', 'source_location', 'destination_location')
    search_fields = ('reference_number',)


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ('batch', 'location', 'reason', 'adjusted_quantity', 'performed_by', 'created_at')
    list_filter = ('reason', 'location')
