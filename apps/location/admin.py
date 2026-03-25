from django.contrib import admin
from .models import Location


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('name', 'location_type', 'manager', 'phone_number', 'is_active')
    list_filter = ('location_type', 'is_active')
    search_fields = ('name', 'address', 'manager__username')