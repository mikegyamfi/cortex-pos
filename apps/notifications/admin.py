from django.contrib import admin
from .models import NotificationTemplate, NotificationLog


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'is_active')


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'type', 'status', 'created_at')
    list_filter = ('status', 'type')
    search_fields = ('recipient',)
    readonly_fields = ('message_content', 'gateway_response', 'error_message')
