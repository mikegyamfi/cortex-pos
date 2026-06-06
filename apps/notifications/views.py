from django.shortcuts import render
from django.contrib.auth.decorators import login_required

from apps.core.decorators import role_required, MANAGEMENT
from .models import NotificationLog


@login_required
@role_required(*MANAGEMENT)
def notification_log(request):
    """
    List history of all SMS/Emails sent (Owner / Manager only).
    """
    logs = NotificationLog.objects.all().select_related('customer', 'sale').order_by('-created_at')[:100]
    return render(request, 'notifications/log_list.html', {'logs': logs})
