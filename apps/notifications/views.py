from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .models import NotificationLog


@login_required
def notification_log(request):
    """
    List history of all SMS/Emails sent.
    """
    # Only Managers/Owners should see logs
    if request.user.role not in ['OWNER', 'MANAGER']:
        return render(request, 'core/403.html')  # Or redirect

    logs = NotificationLog.objects.all().select_related('customer', 'sale').order_by('-created_at')[:50]

    return render(request, 'notifications/log_list.html', {'logs': logs})




