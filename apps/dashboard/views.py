from datetime import timedelta

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Sum, Count, Q, F
from django.http import HttpResponseForbidden

from apps.analytics.models import DailyShopSummary
from apps.core.decorators import role_required, FINANCE_VIEWERS, MANAGEMENT
from apps.sales.models import Sale, SaleItem, SalePayment
from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product


REVENUE_STATUSES = [
    Sale.Status.COMPLETED,
    Sale.Status.PARTIAL_REFUND,
    Sale.Status.REFUNDED,
]


@login_required
def dashboard_router(request):
    """
    Phase 1: The Traffic Controller.
    Decides where to send the user based on their Role.
    """
    user = request.user

    # 1. Cashiers & Salespeople -> Go straight to the POS terminal.
    if user.role in ['CASHIER', 'SALESPERSON']:
        return redirect('sales:pos')

    # 2. Warehouse Staff -> Go to Inventory Ops
    elif user.role == 'WAREHOUSE_STAFF':
        return redirect('inventory:dashboard')

    # 3. Owners, Managers, Accountants -> Go to Analytics
    elif user.role in ['OWNER', 'MANAGER', 'ACCOUNTANT'] or user.is_superuser:
        return redirect('dashboard:analytics')

    else:
        return HttpResponseForbidden()


@login_required
@role_required(*FINANCE_VIEWERS)
def owner_analytics(request):
    """
    Phase 2: The Data Engine (Owner's View).
    Aggregates data for the 'God Mode' dashboard.
    """
    user = request.user

    today = timezone.now().date()

    # --- Context Switching Logic ---
    # Check if the user is filtering by a specific location
    selected_location_id = request.GET.get('location')
    locations = Location.objects.filter(is_active=True)

    if selected_location_id:
        analytics_scope = locations.filter(id=selected_location_id)
        current_view_name = analytics_scope.first().name
    else:
        # Default: View All
        analytics_scope = locations
        current_view_name = "All Locations"

    # --- 1. The Big Numbers (Today) ---
    # Revenue/profit are computed from NON-REFUNDED line items so that
    # partial refunds correctly reduce the metrics.
    todays_items = SaleItem.objects.filter(
        sale__created_at__date=today,
        sale__location__in=analytics_scope,
        sale__status__in=REVENUE_STATUSES,
        is_refunded=False,
    )
    revenue_total = todays_items.aggregate(s=Sum('total_price'))['s'] or 0
    cogs_total = todays_items.annotate(
        line_cost=F('unit_cost') * F('quantity')
    ).aggregate(s=Sum('line_cost'))['s'] or 0

    transactions_count = Sale.objects.filter(
        created_at__date=today,
        status__in=REVENUE_STATUSES,
        location__in=analytics_scope,
    ).count()

    todays_sales = {
        'revenue': revenue_total,
        'transactions': transactions_count,
        'profit': (revenue_total or 0) - (cogs_total or 0),
    }


    # --- 2. Cash Flow (Money in Hand) ---
    # Sum of payments collected today (Cash vs Digital).
    # Negative SalePayments (change-given, refunds) reconcile this naturally.
    payments = SalePayment.objects.filter(
        created_at__date=today,
        sale__location__in=analytics_scope
    ).aggregate(
        cash=Sum('amount', filter=Q(payment_method='CASH')),
        digital=Sum('amount', filter=~Q(payment_method='CASH'))
    )

    # --- 3. Critical Alerts ---
    low_stock_count = StockBatch.objects.filter(
        location__in=analytics_scope,
        quantity__lte=5  # Hardcoded threshold, should come from settings
    ).count()

    # --- 4. Chart Data (Last 7 Days) ---
    # UPDATED: We now query the Sale table directly for real-time updates.
    # This replaces the DailyShopSummary lookup which required an end-of-day process.

    start_date = today - timedelta(days=6)

    # Group non-refunded line items by date so partial refunds reduce the bar.
    sales_data = SaleItem.objects.filter(
        sale__location__in=analytics_scope,
        sale__created_at__date__gte=start_date,
        sale__status__in=REVENUE_STATUSES,
        is_refunded=False,
    ).values('sale__created_at__date').annotate(
        total=Sum('total_price')
    ).order_by('sale__created_at__date')

    # Convert DB result to a Dictionary for easy lookup: { date(...): 500.00 }
    sales_map = {item['sale__created_at__date']: item['total'] for item in sales_data}

    chart_labels = []
    chart_data = []

    # Loop through the last 7 days to ensure even days with 0 sales show up on the chart
    for i in range(6, -1, -1):
        date = today - timedelta(days=i)
        chart_labels.append(date.strftime('%a'))  # Mon, Tue, Wed...
        # Get amount from map or default to 0
        amount = sales_map.get(date, 0)
        chart_data.append(float(amount))

    context = {
        'locations': locations,
        'selected_location_id': int(selected_location_id) if selected_location_id else None,
        'view_name': current_view_name,

        # Big Cards
        'revenue': todays_sales['revenue'] or 0,
        'transactions': todays_sales['transactions'] or 0,
        'profit': todays_sales['profit'] or 0,

        # Cash Flow
        'cash_in_hand': payments['cash'] or 0,
        'digital_sales': payments['digital'] or 0,

        # Alerts
        'low_stock_count': low_stock_count,

        # Charts
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    }

    return render(request, 'dashboard/analytics.html', context)


def _date_range(request):
    """Resolve a start/end date filter from GET (defaults to this month)."""
    today = timezone.now().date()
    start_str = request.GET.get('start_date')
    end_str = request.GET.get('end_date')
    start = timezone.datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else today.replace(day=1)
    end = timezone.datetime.strptime(end_str, "%Y-%m-%d").date() if end_str else today
    return start, end


@login_required
@role_required(*MANAGEMENT)
def business_reports(request):
    """
    Reports hub: headline revenue figures plus links to the detailed reports.
    Owners see all locations; managers see their own.
    """
    user = request.user
    today = timezone.now().date()

    items = SaleItem.objects.filter(sale__status__in=REVENUE_STATUSES, is_refunded=False)
    sales = Sale.objects.filter(status__in=REVENUE_STATUSES)
    if user.role != 'OWNER':
        items = items.filter(sale__location=user.assigned_location)
        sales = sales.filter(location=user.assigned_location)

    def revenue_since(days):
        start = today - timedelta(days=days)
        return items.filter(sale__created_at__date__gte=start).aggregate(s=Sum('total_price'))['s'] or 0

    revenue_today = items.filter(sale__created_at__date=today).aggregate(s=Sum('total_price'))['s'] or 0

    # Top products by revenue over the last 30 days
    top_products = list(
        items.filter(sale__created_at__date__gte=today - timedelta(days=30))
        .values('product__name')
        .annotate(revenue=Sum('total_price'), units=Sum('quantity'))
        .order_by('-revenue')[:5]
    )

    # Payment mix over the last 30 days
    pay_qs = SalePayment.objects.filter(created_at__date__gte=today - timedelta(days=30))
    if user.role != 'OWNER':
        pay_qs = pay_qs.filter(sale__location=user.assigned_location)
    payment_mix = list(pay_qs.values('payment_method').annotate(total=Sum('amount')).order_by('-total'))

    context = {
        'revenue_today': revenue_today,
        'revenue_7d': revenue_since(6),
        'revenue_30d': revenue_since(29),
        'transactions_30d': sales.filter(created_at__date__gte=today - timedelta(days=29)).count(),
        'top_products': top_products,
        'payment_mix': payment_mix,
    }
    return render(request, 'dashboard/reports.html', context)


@login_required
@role_required(*MANAGEMENT)
def staff_performance(request):
    """
    Sales leaderboard by cashier for a date range (defaults to this month).
    Owners see all locations; managers see their own.
    """
    user = request.user
    start, end = _date_range(request)

    items = SaleItem.objects.filter(
        sale__status__in=REVENUE_STATUSES, is_refunded=False,
        sale__created_at__date__range=[start, end],
    )
    sales = Sale.objects.filter(status__in=REVENUE_STATUSES, created_at__date__range=[start, end])
    if user.role != 'OWNER':
        items = items.filter(sale__location=user.assigned_location)
        sales = sales.filter(location=user.assigned_location)

    # Revenue + units sold per cashier
    rev_rows = items.values('sale__cashier_id', 'sale__cashier__username').annotate(
        revenue=Sum('total_price'), units=Sum('quantity')
    )
    # Transaction count per cashier
    txn_map = {r['cashier_id']: r['n'] for r in sales.values('cashier_id').annotate(n=Count('id'))}

    staff = []
    for r in rev_rows:
        cid = r['sale__cashier_id']
        staff.append({
            'name': r['sale__cashier__username'] or 'Unknown',
            'revenue': r['revenue'] or 0,
            'units': r['units'] or 0,
            'transactions': txn_map.get(cid, 0),
        })
    staff.sort(key=lambda s: s['revenue'], reverse=True)

    context = {
        'staff': staff,
        'start_date': start.strftime("%Y-%m-%d"),
        'end_date': end.strftime("%Y-%m-%d"),
    }
    return render(request, 'dashboard/staff_performance.html', context)


