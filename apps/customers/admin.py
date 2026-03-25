from django.contrib import admin
from .models import Customer, CustomerGroup


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('phone_number', 'first_name', 'last_name', 'total_spent', 'total_visits', 'last_visit_date')
    search_fields = ('phone_number', 'first_name', 'last_name', 'email')
    list_filter = ('total_visits',)


@admin.register(CustomerGroup)
class CustomerGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')
