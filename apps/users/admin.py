from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, UserProfile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Detailed Profile'


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    inlines = (UserProfileInline,)
    list_display = ('username', 'get_full_name', 'role', 'assigned_location', 'is_active')
    list_filter = ('role', 'assigned_location', 'is_active', 'requires_password_change')

    # Extend standard UserAdmin fieldsets
    fieldsets = UserAdmin.fieldsets + (
        ('Retail Permissions', {'fields': ('role', 'assigned_location', 'requires_password_change')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Retail Permissions', {'fields': ('role', 'assigned_location', 'email')}),
    )