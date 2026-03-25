from django.contrib import admin
from .models import Sale, SaleItem, SalePayment, SaleTax, RegisterSession, Delivery


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    readonly_fields = ('total_price', 'unit_price', 'unit_cost')
    can_delete = False


class SalePaymentInline(admin.TabularInline):
    model = SalePayment
    extra = 0
    can_delete = False


class SaleTaxInline(admin.TabularInline):
    model = SaleTax
    extra = 0
    readonly_fields = ('tax_amount',)


class DeliveryInline(admin.StackedInline):
    model = Delivery
    can_delete = False
    verbose_name_plural = 'Logistics & Delivery'


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    inlines = [SaleItemInline, SalePaymentInline, SaleTaxInline, DeliveryInline]
    list_display = ('invoice_number', 'location', 'total_amount', 'status', 'payment_method_display', 'created_at')
    list_filter = ('status', 'location', 'created_at', 'order_type')
    search_fields = ('invoice_number', 'customer__phone_number', 'customer__first_name')
    readonly_fields = ('invoice_number', 'subtotal', 'total_tax', 'total_amount', 'created_at', 'amount_paid',
                       'change_due')

    def payment_method_display(self, obj):
        # Since payment_method was removed from Sale model in favor of SalePayment model,
        # we can display a summary of methods used or just the count.
        methods = obj.payments.values_list('payment_method', flat=True).distinct()
        return ", ".join(methods) if methods else "Unpaid"

    payment_method_display.short_description = "Payment Methods"


@admin.register(RegisterSession)
class RegisterSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'location', 'start_time', 'end_time', 'status', 'discrepancy')
    list_filter = ('status', 'location', 'start_time')
    search_fields = ('user__username', 'location__name')
    readonly_fields = ('discrepancy', 'total_cash_sales', 'total_momo_sales', 'total_card_sales')


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ('sale', 'provider', 'status', 'rider_name', 'updated_at')
    list_filter = ('status', 'provider')
    search_fields = ('sale__invoice_number', 'rider_name', 'tracking_reference')
