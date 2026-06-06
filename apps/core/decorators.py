"""
Reusable authorization decorators for role-based access control.

The project uses a custom role string on the User model
(`apps.users.models.User.Role`) rather than Django's Groups/Permissions,
so these decorators centralise the role checks that were previously
scattered (and inconsistently applied) across the view layer.

Usage:

    from apps.core.decorators import role_required, MANAGEMENT

    @role_required(*MANAGEMENT)
    def my_view(request):
        ...

Superusers always pass. For normal page views an unauthorised user is
redirected to their own home dashboard with an error message. For
endpoints that return JSON (the POS APIs), pass ``api=True`` to get a
403 JSON response instead of a redirect.
"""
from functools import wraps

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect


# --- Role bundles ---------------------------------------------------------
# Defined as plain string tuples so they can be spread into role_required()
# and reused anywhere without importing the User model.
OWNER = 'OWNER'
MANAGER = 'MANAGER'
WAREHOUSE_STAFF = 'WAREHOUSE_STAFF'
SALESPERSON = 'SALESPERSON'
CASHIER = 'CASHIER'
ACCOUNTANT = 'ACCOUNTANT'

# Owner + Manager: full operational control of a location.
MANAGEMENT = (OWNER, MANAGER)
# Who may read the books / financial reports.
FINANCE_VIEWERS = (OWNER, MANAGER, ACCOUNTANT)
# Who may touch stock (receive, transfer, adjust, view batches).
INVENTORY_STAFF = (OWNER, MANAGER, WAREHOUSE_STAFF)
# Who may sell / use the POS and handle deliveries.
SELLING_STAFF = (OWNER, MANAGER, CASHIER, SALESPERSON)


def role_required(*roles, api=False):
    """
    Restrict a view to the given role strings.

    Superusers bypass the check. Unauthorised users get a redirect (page
    views) or a 403 JSON body (when ``api=True``).
    """
    allowed = set(roles)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if user.is_superuser or getattr(user, 'role', None) in allowed:
                return view_func(request, *args, **kwargs)

            if api:
                return JsonResponse(
                    {'success': False, 'message': 'You are not authorised to perform this action.'},
                    status=403,
                )
            messages.error(request, "You do not have permission to access that page.")
            return redirect('dashboard:index')

        return _wrapped

    return decorator