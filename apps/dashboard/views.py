from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponseForbidden

from apps.analytics.models import DailyShopSummary
from apps.core.decorators import role_required, FINANCE_VIEWERS, MANAGEMENT
from apps.core.search import search_queryset
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


def _parse_date(raw):
    try:
        return datetime.strptime((raw or '').strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_range(request, default_days=None):
    """
    Resolve a start/end date filter from GET. Defaults to this month, or to the
    last `default_days` days (today included). A mistyped date falls back to
    the default instead of crashing the page; a reversed range is swapped.
    """
    today = timezone.localdate()
    default_start = today - timedelta(days=default_days - 1) if default_days else today.replace(day=1)
    start = _parse_date(request.GET.get('start_date')) or default_start
    end = _parse_date(request.GET.get('end_date')) or today
    if start > end:
        start, end = end, start
    return start, end


def reports_start():
    """
    The first day the reports count (settings.REPORTS_START_DATE, from the
    REPORTS_START_DATE config var), or None to count everything.

    Lets the business start its reports afresh from a clean day without
    deleting the earlier sales, which stay in the sales history.
    """
    return _parse_date(getattr(settings, 'REPORTS_START_DATE', '') or '')


LINE_COST = ExpressionWrapper(F('unit_cost') * F('quantity'),
                             output_field=DecimalField(max_digits=14, decimal_places=2))


def _products_sold(items, sort='revenue'):
    """
    Group sale lines by PRODUCT (never by name — every shop has its own
    same-named copy) into units / revenue / cost / profit / number of sales.
    Returns (rows queryset, totals over all rows).
    """
    rows = (
        items.values('product_id', 'product__name', 'product__sku', 'product__location__name')
        .annotate(units=Sum('quantity'), revenue=Sum('total_price'), cost=Sum(LINE_COST),
                  sale_count=Count('sale', distinct=True))
        .annotate(profit=F('revenue') - F('cost'))
        .order_by(*PRODUCT_SORTS[sort])
    )
    totals = items.aggregate(units=Sum('quantity'), revenue=Sum('total_price'), cost=Sum(LINE_COST))
    totals = {k: v or 0 for k, v in totals.items()}
    totals['profit'] = totals['revenue'] - totals['cost']
    return rows, totals


PRODUCT_SORTS = {
    'revenue': ('-revenue', 'product__name'),
    'units': ('-units', 'product__name'),
    'profit': ('-profit', 'product__name'),
    'name': ('product__name', 'product__location__name'),
}


@login_required
@role_required(*MANAGEMENT)
def business_reports(request):
    """
    Reports hub: headline revenue, every product sold in a period, payment mix.

    Owners see all shops (or one, via ?location=); managers see their own.

    Products are grouped by the product itself, never by name: each shop has
    its own copy of a product, and grouping by name merged them — a shop that
    sold 8 showed 11 because another shop sold 3 of a same-named product.
    """
    user = request.user
    today = timezone.localdate()
    is_owner = user.role == 'OWNER' or user.is_superuser

    # ---- scope -------------------------------------------------------------
    locations = Location.objects.filter(is_active=True).order_by('name') if is_owner else Location.objects.none()
    location = None
    if is_owner:
        loc_id = request.GET.get('location') or ''
        if loc_id.isdigit():
            location = locations.filter(pk=loc_id).first()
    else:
        location = user.assigned_location

    items = SaleItem.objects.filter(sale__status__in=REVENUE_STATUSES, is_refunded=False)
    sales = Sale.objects.filter(status__in=REVENUE_STATUSES)
    payments = SalePayment.objects.all()
    if location is not None or not is_owner:
        items = items.filter(sale__location=location)
        sales = sales.filter(location=location)
        payments = payments.filter(sale__location=location)

    # Nothing before the reports start date counts on this page.
    floor = reports_start()
    if floor is not None:
        items = items.filter(sale__created_at__date__gte=floor)
        sales = sales.filter(created_at__date__gte=floor)
        payments = payments.filter(created_at__date__gte=floor)

    def clamp(first, last):
        if floor is not None:
            first, last = max(first, floor), max(last, floor)
        return first, last

    def revenue_between(first, last):
        return items.filter(sale__created_at__date__range=[first, last]) \
                    .aggregate(s=Sum('total_price'))['s'] or Decimal('0.00')

    last_7 = today - timedelta(days=6)
    last_30 = today - timedelta(days=29)

    # ---- the selected period (defaults to today) -----------------------------
    start, end = clamp(*_date_range(request, default_days=1))
    yesterday = today - timedelta(days=1)
    presets, seen = [], set()
    for label, first, last in (
        ('Today', today, today),
        ('Yesterday', yesterday, yesterday),
        ('Last 7 days', last_7, today),
        ('Last 30 days', last_30, today),
        ('This month', today.replace(day=1), today),
    ):
        if floor is not None and last < floor:
            continue                       # entirely before the start date
        first, last = clamp(first, last)
        if (first, last) in seen:
            continue                       # same days as a button already shown
        seen.add((first, last))
        presets.append({'label': label, 'start': first.isoformat(), 'end': last.isoformat(),
                        'active': (first, last) == (start, end)})

    period_items = items.filter(sale__created_at__date__range=[start, end])
    period_sales = sales.filter(created_at__date__range=[start, end])
    period_payments = payments.filter(created_at__date__range=[start, end])

    # ---- the period at a glance -------------------------------------------------
    _, all_totals = _products_sold(period_items)
    sale_money = period_sales.aggregate(n=Count('id'), billed=Sum('total_amount'), paid=Sum('amount_paid'))
    billed = sale_money['billed'] or Decimal('0.00')
    paid = sale_money['paid'] or Decimal('0.00')
    count = sale_money['n'] or 0
    summary = {
        'revenue': all_totals['revenue'],
        'profit': all_totals['profit'],
        'margin': (all_totals['profit'] / all_totals['revenue'] * 100) if all_totals['revenue'] else None,
        'units': all_totals['units'],
        'sales': count,
        'average': (billed / count) if count else Decimal('0.00'),
        'owed': billed - paid,
        'collected': period_payments.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
    }

    # Day by day, so a multi-day period can be read (and each day opened).
    by_day = []
    if start != end:
        rows = {r['day']: r for r in period_items.annotate(day=TruncDate('sale__created_at'))
                .values('day').annotate(revenue=Sum('total_price'), units=Sum('quantity'),
                                        sales=Count('sale', distinct=True))}
        d = end
        while d >= start:
            r = rows.get(d, {})
            by_day.append({'day': d, 'revenue': r.get('revenue') or Decimal('0.00'),
                           'units': r.get('units') or 0, 'sales': r.get('sales') or 0})
            d -= timedelta(days=1)

    query = (request.GET.get('q') or '').strip()
    if query:
        # Search the sale lines, then group (the search's typo pass needs rows with a pk).
        period_items = search_queryset(period_items, query, ['product__name', 'product__sku'])

    sort = request.GET.get('sort') if request.GET.get('sort') in PRODUCT_SORTS else 'revenue'
    products, totals = _products_sold(period_items, sort)

    page_obj = Paginator(products, 25).get_page(request.GET.get('page'))

    # Payment mix for the same period and scope. Change and refunds are
    # negative rows, so each method nets to the money actually kept.
    method_labels = dict(SalePayment.PaymentMethod.choices)
    payment_mix = [
        {'method': method_labels.get(r['payment_method'], r['payment_method']), 'total': r['total']}
        for r in period_payments.values('payment_method').annotate(total=Sum('amount')).order_by('-total')
    ]

    params = request.GET.copy()
    params.pop('page', None)

    context = {
        'revenue_today': revenue_between(today, today),
        'revenue_7d': revenue_between(last_7, today),
        'revenue_30d': revenue_between(last_30, today),
        'transactions_30d': sales.filter(created_at__date__range=[last_30, today]).count(),
        'summary': summary,
        'by_day': by_day,
        'reports_start': floor,
        'page_obj': page_obj,
        'totals': totals,
        'payment_mix': payment_mix,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'start': start,
        'end': end,
        'query': query,
        'sort': sort,
        'is_owner': is_owner,
        'locations': locations,
        'location': location,
        'querystring': params.urlencode(),
        'presets': presets,
        'period_is_today': start == end == today,
    }
    return render(request, 'dashboard/reports.html', context)


# Methods that have a drawer bucket; the rest (bank, cheque) go straight to the bank.
DAY_METHODS = ['CASH', 'MOMO', 'CARD', 'BANK', 'CHEQUE']


def _money_row(label, help_text, rows):
    """{label, help, CASH: n, MOMO: n, ..., total: n} from (method, amount) pairs."""
    row = {'label': label, 'help': help_text, 'by_method': {m: Decimal('0.00') for m in DAY_METHODS},
           'total': Decimal('0.00')}
    for method, amount in rows:
        if method in row['by_method']:
            row['by_method'][method] += amount
        row['total'] += amount
    row['cells'] = [row['by_method'][m] for m in DAY_METHODS]
    return row


@login_required
@role_required(*MANAGEMENT)
def daily_report(request):
    """
    One day at one shop, end to end, so it can be checked line by line:

      1. Sales made that day, and how each bill stands (paid + owed = billed).
      2. Every movement of money that day, by kind and method — takings,
         debt collected, change handed back, refunds paid out.
      3. Each drawer used that day: float, expected cash, counted, variance.
      4. What was sold.
      5. The same checks as `manage.py reconcile_sales`, run for that day only,
         so the page itself says whether the day tallies.
    """
    from apps.finance.models import Expense
    from apps.sales.models import RegisterSession
    from apps.sales.reconcile import Finding, reconcile

    user = request.user
    is_owner = user.role == 'OWNER' or user.is_superuser
    today = timezone.localdate()
    day = _parse_date(request.GET.get('date')) or today
    if day > today:
        day = today

    locations = Location.objects.filter(is_active=True).order_by('name') if is_owner else Location.objects.none()
    location = None
    if is_owner:
        loc_id = request.GET.get('location') or ''
        if loc_id.isdigit():
            location = locations.filter(pk=loc_id).first()
    else:
        location = user.assigned_location

    def scoped(qs, path):
        return qs.filter(**{path: location}) if (location is not None or not is_owner) else qs

    # ---- 1. sales made today -------------------------------------------------
    day_sales = scoped(Sale.objects.filter(status__in=REVENUE_STATUSES, created_at__date=day), 'location')
    sale_totals = day_sales.aggregate(n=Count('id'), billed=Sum('total_amount'), paid=Sum('amount_paid'))
    billed = sale_totals['billed'] or Decimal('0.00')
    paid = sale_totals['paid'] or Decimal('0.00')
    day_items = SaleItem.objects.filter(sale__in=day_sales)
    refunded_value = day_items.filter(is_refunded=True).aggregate(s=Sum('total_price'))['s'] or Decimal('0.00')
    kept_items = day_items.filter(is_refunded=False)
    units = kept_items.aggregate(s=Sum('quantity'))['s'] or 0
    credit_sales = day_sales.filter(amount_paid__lt=F('total_amount'))
    sales_summary = {
        'count': sale_totals['n'] or 0,
        'units': units,
        'gross': billed + refunded_value,
        'refunded': refunded_value,
        'billed': billed,
        'paid': paid,
        'owed': billed - paid,
        'credit_count': credit_sales.count(),
        'cancelled': scoped(Sale.objects.filter(status=Sale.Status.CANCELLED, created_at__date=day), 'location').count(),
    }

    # ---- 2. money that moved today ------------------------------------------
    pays = scoped(SalePayment.objects.filter(created_at__date=day), 'sale__location').select_related('sale')
    buckets = {'sales': [], 'debt': [], 'earlier': [], 'change': [], 'refund': []}
    for p in pays:
        ref = p.reference_id or ''
        if ref == 'CHANGE GIVEN':
            buckets['change'].append((p.payment_method, p.amount))
        elif ref.startswith('REFUND'):
            buckets['refund'].append((p.payment_method, p.amount))
        elif p.is_settlement:
            buckets['debt'].append((p.payment_method, p.amount))
        elif timezone.localtime(p.sale.created_at).date() == day:
            buckets['sales'].append((p.payment_method, p.amount))
        else:
            buckets['earlier'].append((p.payment_method, p.amount))
    money_rows = [
        _money_row('Taken on today\'s sales', 'tendered at the till', buckets['sales']),
        _money_row('Debt collected', 'settlements of earlier credit sales', buckets['debt']),
        _money_row('Other payments on earlier sales', 'not marked as settlements', buckets['earlier']),
        _money_row('Change given', 'handed back in cash', buckets['change']),
        _money_row('Refunds paid out', 'money returned to customers', buckets['refund']),
    ]
    money_rows = [r for r in money_rows if any(r['cells']) or r is money_rows[0]]
    money_net = _money_row('Net money in', '', [(m, a) for r in money_rows for m, a in r['by_method'].items()])

    # ---- 3. drawers used today ----------------------------------------------
    sessions = scoped(
        RegisterSession.objects.filter(start_time__date__lte=day)
        .filter(Q(end_time__isnull=True) | Q(end_time__date__gte=day)), 'location'
    ).select_related('user', 'location').order_by('start_time')
    drawers = []
    for s in sessions:
        expenses_qs = Expense.objects.filter(location=s.location, is_paid_from_till=True,
                                             status=Expense.Status.APPROVED, created_at__gte=s.start_time)
        if s.end_time:
            expenses_qs = expenses_qs.filter(created_at__lte=s.end_time)
        till_expenses = expenses_qs.aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
        is_open = s.status == RegisterSession.Status.OPEN
        expected = (s.opening_balance + s.total_cash_sales - till_expenses) if is_open else s.closing_balance_expected
        started = timezone.localtime(s.start_time).date()
        ended = timezone.localtime(s.end_time).date() if s.end_time else None
        drawers.append({
            'session': s,
            'till_expenses': till_expenses,
            'expected': expected,
            'counted': s.closing_balance_actual,
            'variance': (s.closing_balance_actual - expected) if s.closing_balance_actual is not None else None,
            'is_open': is_open,
            # A shift that runs over several days mixes their cash together:
            # this day's drawer cannot be checked on its own.
            'multi_day': started != (ended or today),
            'started': started,
        })

    # ---- 4. what was sold -----------------------------------------------------
    products, product_totals = _products_sold(kept_items, 'revenue')

    # ---- 5. does the day tally? --------------------------------------------------
    issues = reconcile(fix=False, location=location, since=day, until=day,
                       include_customers=False).findings
    for d in drawers:
        if d['multi_day']:
            s = d['session']
            span = f"{d['started']:%d %b}" + (f" to {timezone.localtime(s.end_time):%d %b}" if s.end_time else ' and still open')
            issues.append(Finding(
                'long-shift', f'Shift #{s.id} ({s.user})',
                f"ran from {span}, so its cash covers more than this day. Close a shift every day "
                f"so each day's drawer can be counted on its own.",
                fixable=False,
            ))

    return render(request, 'dashboard/daily_report.html', {
        'day': day,
        'prev_day': day - timedelta(days=1),
        'next_day': day + timedelta(days=1) if day < today else None,
        'is_today': day == today,
        'reports_start': reports_start(),
        'before_start': bool(reports_start() and day < reports_start()),
        'is_owner': is_owner,
        'locations': locations,
        'location': location,
        'sales_summary': sales_summary,
        'credit_sales': credit_sales.select_related('customer').order_by('created_at'),
        'methods': [dict(SalePayment.PaymentMethod.choices)[m] for m in DAY_METHODS],
        'money_rows': money_rows,
        'money_net': money_net,
        'drawers': drawers,
        'products': products,
        'product_totals': product_totals,
        'issues': issues,
    })


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


