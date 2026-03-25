from datetime import timedelta

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Sum, Count, Q
from django.http import HttpResponseForbidden

from apps.analytics.models import DailyShopSummary
from apps.sales.models import Sale, SalePayment
from apps.inventory.models import StockBatch
from apps.location.models import Location


@login_required
def dashboard_router(request):
    """
    Phase 1: The Traffic Controller.
    Decides where to send the user based on their Role.
    """
    user = request.user

    # 1. Cashiers & Salespeople -> Go straight to POS/Sales
    if user.role in ['CASHIER', 'SALESPERSON']:
        # If they haven't opened a register session, the POS view will handle that check
        # return redirect('sales:pos')
        return redirect('dashboard:analytics')

    # 2. Warehouse Staff -> Go to Inventory Ops
    elif user.role == 'WAREHOUSE_STAFF':
        return redirect('dashboard:analytics')

    # 3. Owners, Managers, Accountants -> Go to Analytics
    elif user.role in ['OWNER', 'MANAGER', 'ACCOUNTANT'] or user.is_superuser:
        return redirect('dashboard:analytics')

    else:
        return HttpResponseForbidden()


@login_required
def owner_analytics(request):
    """
    Phase 2: The Data Engine (Owner's View).
    Aggregates data for the 'God Mode' dashboard.
    """
    user = request.user
    if user.role not in ['OWNER', 'MANAGER', 'ACCOUNTANT']:
        return redirect('dashboard:index')

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
    # We aggregate real-time sales for TODAY (Live Pulse)
    todays_sales = Sale.objects.filter(
        created_at__date=today,
        status=Sale.Status.COMPLETED,
        location__in=analytics_scope
    ).aggregate(
        revenue=Sum('total_amount'),
        transactions=Count('id'),
        profit=Sum('items__total_price') - Sum('items__unit_cost')  # Simplified Gross Profit
    )

    # --- 2. Cash Flow (Money in Hand) ---
    # Sum of payments collected today (Cash vs Digital)
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

    # Get sales grouped by date
    sales_data = Sale.objects.filter(
        location__in=analytics_scope,
        created_at__date__gte=start_date,
        status=Sale.Status.COMPLETED
    ).values('created_at__date').annotate(
        total=Sum('total_amount')
    ).order_by('created_at__date')

    # Convert DB result to a Dictionary for easy lookup: { '2023-10-01': 500.00 }
    sales_map = {item['created_at__date']: item['total'] for item in sales_data}

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


