from django.contrib import admin
from .models import Tax, ExpenseCategory, Expense, RevenueTarget


@admin.register(Tax)
class TaxAdmin(admin.ModelAdmin):
    list_display = ('name', 'rate', 'tax_type', 'is_active', 'priority')
    list_editable = ('is_active', 'rate')


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('category', 'amount', 'location', 'status', 'requested_by', 'date_incurred')
    list_filter = ('status', 'location', 'category')
    search_fields = ('description',)


@admin.register(RevenueTarget)
class RevenueTargetAdmin(admin.ModelAdmin):
    list_display = ('location', 'period', 'target_amount', 'start_date', 'end_date', 'is_active')
    list_filter = ('location', 'period', 'is_active')
