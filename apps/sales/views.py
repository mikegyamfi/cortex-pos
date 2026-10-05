import json
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q, F, Sum
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Sale, SaleItem, SaleTax, RegisterSession, SalePayment, Delivery
from ..core.decorators import role_required, SELLING_STAFF, MANAGEMENT
from ..core.search import search_queryset
from ..customers.models import Customer
from ..finance.models import Expense
from ..inventory.models import StockBatch, StockAdjustment
from ..location.models import Location
from ..notifications.services import SMSService
from ..products.models import Category, Product
from .services import (
    SaleError, parse_money, receipt_lines, receipt_lines_for_sale, record_refund,
    record_sale, record_settlement, resolve_cart,
)


TWO_PLACES = Decimal('0.01')


def _q(value):
    """Quantize a Decimal to 2dp (banker-safe rounding)."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _open_session(user, location, lock=False):
    """
    The user's open drawer at `location`, oldest first so every request picks
    the same one. With lock=True the row is held until the transaction ends,
    which serialises checkouts on one till (no lost drawer updates, and a
    retried checkout sees the first attempt's sale).
    """
    qs = RegisterSession.objects.filter(
        user=user, location=location, status=RegisterSession.Status.OPEN,
    ).order_by('start_time', 'id')
    if lock:
        qs = qs.select_for_update()
    return qs.first()


def _sale_response(sale, lines, duplicate=False):
    return JsonResponse({
        'success': True,
        'duplicate': duplicate,
        'invoice_number': sale.invoice_number,
        'sale_id': sale.id,
        'total_amount': float(sale.total_amount),
        'amount_paid': float(sale.amount_paid),
        'change_due': float(sale.change_due),
        'balance_due': float(sale.balance_remaining),
        'lines': lines,
    })


def _location_stock_map(location, product_ids=None):
    """
    Return {product_id: quantity_on_hand} for the given location, summing
    across all positive-quantity batches. Used by the POS to show live
    stock per product.
    """
    qs = StockBatch.objects.filter(location=location, quantity__gt=0)
    if product_ids is not None:
        qs = qs.filter(product_id__in=product_ids)
    rows = qs.values('product_id').annotate(qty=Sum('quantity'))
    return {r['product_id']: r['qty'] for r in rows}


@login_required
@role_required(*SELLING_STAFF)
def pos_view(request):
    """
    The Cashier's Cockpit.
    1. Checks if a Register Session is OPEN.
    2. If not, forces them to open one.
    3. Renders the POS interface with Categories and Products.
    """
    user = request.user
    location = user.assigned_location

    # 1. Check for Active Session
    active_session = _open_session(user, location)

    if not active_session:
        if request.method == 'POST':
            try:
                opening = parse_money(request.POST.get('opening_balance'), 'Opening float')
                if opening < 0:
                    raise SaleError('Opening float cannot be negative.')
            except SaleError as err:
                messages.error(request, str(err))
                return render(request, 'sales/open_register.html')
            with transaction.atomic():
                # Re-check inside the transaction so two tabs can't open two
                # drawers (sales would then be split between them).
                if not _open_session(user, location, lock=True):
                    RegisterSession.objects.create(user=user, location=location, opening_balance=opening)
            return redirect('sales:pos')
        return render(request, 'sales/open_register.html')

    # 2. Load Catalog Data for POS (with live stock for THIS location so the
    #    cashier can see quantity-on-hand on every product card). Products are
    #    scoped to this shop — a register only sells its own shop's catalogue.
    categories = Category.objects.filter(is_active=True)
    products = list(
        Product.objects.filter(is_active=True, location=location)
        .select_related('category').order_by('name')
    )
    stock_map = _location_stock_map(location, [p.id for p in products])
    for p in products:
        p.stock_qty = stock_map.get(p.id, 0)

    return render(request, 'sales/pos.html', {
        'session': active_session,
        'location': location,
        'categories': categories,
        'products': products
    })


@login_required
@role_required(*SELLING_STAFF)
def product_search_api(request):
    """
    Server-side product search for the POS.

    Searches the WHOLE active catalogue (name / SKU / barcode) — not just
    the products already rendered in the grid — and returns the live stock
    on hand at the cashier's location for each match.
    """
    location = request.user.assigned_location
    query = (request.GET.get('q') or '').strip()

    # Scoped to the cashier's shop — search never crosses into other shops.
    products = Product.objects.filter(is_active=True, location=location).select_related('category')
    if query:
        products = search_queryset(products, query, ['name', 'sku', 'barcode'])
    products = list(products.order_by('name')[:50])

    stock_map = _location_stock_map(location, [p.id for p in products])

    results = [{
        'id': p.id,
        'name': p.name,
        'sku': p.sku,
        'barcode': p.barcode or '',
        'category_id': p.category_id,
        # Every tier the product is actually priced for. A tier that is not set
        # comes back as null so the POS can grey it out instead of guessing.
        'prices': {
            'RETAIL': float(p.selling_price),
            'WHOLESALE': float(p.wholesale_price) if p.wholesale_price is not None else None,
            'DISTRIBUTOR': float(p.distributor_price) if p.distributor_price is not None else None,
        },
        'stock': int(stock_map.get(p.id, 0)),
    } for p in products]

    return JsonResponse({'results': results})


@login_required
@role_required(*SELLING_STAFF)
@require_POST
@transaction.atomic
def process_sale(request):
    """
    POS checkout. A thin wrapper over apps.sales.services — the browser chooses
    a price LIST per line and the service looks every amount up in the catalogue.
    """
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'message': 'Malformed request.'}, status=400)

    if not isinstance(data, dict):
        return JsonResponse({'success': False, 'message': 'Malformed request.'}, status=400)

    cart = data.get('cart', [])
    payments = data.get('payments', [])
    customer_id = data.get('customer_id')
    client_ref = data.get('client_ref') or None
    if client_ref is not None and (not isinstance(client_ref, str) or len(client_ref) > 64):
        return JsonResponse({'success': False, 'message': 'Malformed request.'}, status=400)

    if not cart:
        return JsonResponse({'success': False, 'message': 'Cart is empty.'}, status=400)

    user = request.user
    location = user.assigned_location

    # Locking the drawer serialises checkouts on this till: a double click or
    # a retry waits for the first request, then finds its sale below.
    session = _open_session(user, location, lock=True)
    if not session:
        return JsonResponse(
            {'success': False, 'message': 'No active register session. Please open register.'},
            status=400,
        )

    if client_ref:
        existing = Sale.objects.filter(client_ref=client_ref).first()
        if existing is not None:
            if existing.cashier_id != user.id:
                return JsonResponse({'success': False, 'message': 'Malformed request.'}, status=400)
            # Already recorded — hand back the original, never sell it twice.
            return _sale_response(existing, receipt_lines_for_sale(existing), duplicate=True)

    customer = None
    if customer_id:
        # Staff may only bill their own shop's customers (or legacy customers
        # not yet bound to any shop) — never another shop's account.
        customers = Customer.objects.all()
        if user.role != 'OWNER' and not user.is_superuser:
            customers = customers.filter(Q(location=location) | Q(location__isnull=True))
        customer = customers.filter(id=customer_id).first()
        if customer is None:
            return JsonResponse({'success': False, 'message': 'Customer not found.'}, status=400)

    try:
        resolved, qty_per_product = resolve_cart(cart, location)
        sale = record_sale(
            location=location, user=user, session=session, customer=customer,
            resolved=resolved, qty_per_product=qty_per_product, payments=payments,
            client_ref=client_ref,
        )
    except SaleError as err:
        transaction.set_rollback(True)
        return JsonResponse({'success': False, 'message': str(err)}, status=400)
    except Exception:
        transaction.set_rollback(True)
        return JsonResponse(
            {'success': False, 'message': 'The sale could not be saved. Nothing was charged — please try again.'},
            status=400,
        )

    return _sale_response(sale, receipt_lines(resolved))


@login_required
@role_required(*SELLING_STAFF)
def sale_list(request):
    """
    Transaction History with Filters.
    Includes Debt/Arrears filtering and Owner "God Mode".
    """
    user = request.user

    # Base Query: Owner sees all, Staff sees assigned location
    if user.role == 'OWNER':
        sales = Sale.objects.all()
        # Optional: Filter by specific location if passed in GET
        location_filter = request.GET.get('location')
        if location_filter:
            sales = sales.filter(location_id=location_filter)
    else:
        sales = Sale.objects.filter(location=user.assigned_location)

    sales = sales.select_related('customer', 'cashier', 'location').order_by('-created_at')

    # 1. Search (Invoice or Customer)
    query = request.GET.get('q')
    if query:
        sales = search_queryset(sales, query, [
            'invoice_number', 'customer__phone_number',
            'customer__first_name', 'customer__last_name',
        ])

    # 2. Status Filter (Enhanced for Debt)
    status = request.GET.get('status')
    if status:
        if status == 'DEBT':
            # Find sales where amount paid is less than total amount
            sales = sales.filter(amount_paid__lt=F('total_amount'))
        else:
            sales = sales.filter(status=status)

    # 3. Single-day filter. Defaults to today, so the page opens on today's
    #    trading. An explicit empty value (?date=) widens it to all time, and a
    #    search without a date looks across the whole history.
    if 'date' in request.GET:
        date_filter = request.GET['date'].strip()
    elif query:
        date_filter = ''
    else:
        date_filter = timezone.localdate().isoformat()

    if date_filter:
        sales = sales.filter(created_at__date=date_filter)

    # 4. Money totals for whatever is on screen.
    #    total_amount / amount_paid are already net of refunds (a refund reduces
    #    both), so these read as real money for the period.
    totals = sales.aggregate(
        billed=Sum('total_amount'),
        collected=Sum('amount_paid'),
    )
    total_billed = totals['billed'] or Decimal('0.00')
    total_collected = totals['collected'] or Decimal('0.00')

    summary = {
        'count': sales.count(),
        'billed': total_billed,
        'collected': total_collected,
        'outstanding': max(total_billed - total_collected, Decimal('0.00')),
    }

    # Context for Owner Location Filter
    locations = []
    if user.role == 'OWNER':
        locations = Location.objects.filter(is_active=True)

    return render(request, 'sales/sale_list.html', {
        'sales': sales,
        'summary': summary,
        'today': timezone.localdate().isoformat(),
        'filters': {
            'q': query,
            'status': status,
            'date': date_filter,
            'location': request.GET.get('location')
        },
        'locations': locations
    })


@login_required
@role_required(*SELLING_STAFF)
def sale_detail(request, pk):
    """
    View Receipt / Sale Details.

    Owners (and superusers) see any receipt; everyone else is restricted to
    receipts from their own assigned location.
    """
    sale = get_object_or_404(Sale, pk=pk)

    user = request.user
    if user.role != 'OWNER' and not user.is_superuser:
        if sale.location_id != user.assigned_location_id:
            messages.error(request, "You can only view receipts from your own location.")
            return redirect('sales:list')

    payments = sale.payments.select_related('processed_by').order_by('created_at')
    return render(request, 'sales/sale_detail.html', {'sale': sale, 'payments': payments})


@login_required
@role_required(*SELLING_STAFF)
def session_list(request):
    """
    List of cashier shifts (for closing/reconciling).
    """
    sessions = RegisterSession.objects.filter(location=request.user.assigned_location).order_by('-created_at')
    return render(request, 'sales/session_list.html', {'sessions': sessions})


@login_required
@role_required(*SELLING_STAFF)
def close_register_view(request):
    """
    End of Shift Logic.
    1. Cashier counts physical money.
    2. Enters totals.
    3. System calculates variance.
    """
    user = request.user
    location = user.assigned_location

    # Get the active session
    session = _open_session(user, location)

    if not session:
        messages.error(request, "No open register session found.")
        return redirect('sales:sessions')

    # Cash expenses taken FROM this drawer during the shift reduce the cash we
    # expect to count. Without this, recording "petty cash" spends would show
    # up as a false drawer shortage (discrepancy) at close.
    # NOTE: this assumes one open drawer per location at a time (the normal
    # single-till setup). Approved + paid-from-till expenses logged at this
    # location after the shift opened are deducted.
    till_expenses = Expense.objects.filter(
        location=location,
        is_paid_from_till=True,
        status=Expense.Status.APPROVED,
        created_at__gte=session.start_time,
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

    # Calculate Expected Totals: Opening Float + Cash Sales - Cash Expenses
    expected_cash = session.opening_balance + session.total_cash_sales - till_expenses

    if request.method == 'POST':
        # Get actual counts from form
        notes = request.POST.get('notes', '')

        # A mistyped count used to become 0.00 silently and close the shift
        # with a fake shortage. Refuse it instead.
        raw = (request.POST.get('actual_cash') or '').strip()
        try:
            if not raw:
                raise SaleError('Enter the cash you counted.')
            actual_cash = parse_money(raw, 'Counted cash')
            if actual_cash < 0:
                raise SaleError('Counted cash cannot be negative.')
        except SaleError as err:
            messages.error(request, str(err))
            return redirect('sales:close_register')

        with transaction.atomic():
            session = RegisterSession.objects.select_for_update().get(pk=session.pk)
            if session.status != RegisterSession.Status.OPEN:
                messages.info(request, "This register was already closed.")
                return redirect('sales:sessions')
            # Re-read the drawer under lock so a sale finishing at the same
            # moment is counted in what we expect.
            expected_cash = session.opening_balance + session.total_cash_sales - till_expenses

            session.closing_balance_expected = expected_cash
            session.closing_balance_actual = actual_cash
            session.end_time = timezone.now()
            session.notes = notes

            # Determine Status (Discrepancy Check)
            if actual_cash != expected_cash:
                session.status = RegisterSession.Status.DISCREPANCY
            else:
                session.status = RegisterSession.Status.CLOSED

            session.save()

        messages.success(request, "Register closed successfully.")
        return redirect('sales:sessions')

    return render(request, 'sales/close_register.html', {
        'session': session,
        'expected_cash': expected_cash,
        'cash_sales': session.total_cash_sales,
        'till_expenses': till_expenses,
    })


@login_required
@role_required(*SELLING_STAFF)
def session_detail(request, pk):
    """
    Detailed Report of a Cashier Shift (Session).
    Shows financial reconciliation and discrepancy.
    """
    session = get_object_or_404(RegisterSession, pk=pk)

    # Security: Ensure user can see this session (Own session or Manager/Owner)
    if request.user.role not in ['OWNER', 'MANAGER', 'ACCOUNTANT'] and session.user != request.user:
        messages.error(request, "You do not have permission to view this report.")
        return redirect('sales:sessions')

    # Get all sales in this session
    sales = session.sales.select_related('customer').order_by('-created_at')

    context = {
        'session': session,
        'sales': sales,
    }
    return render(request, 'sales/session_detail.html', context)


@login_required
@role_required(*SELLING_STAFF)
@require_POST
@transaction.atomic
def add_payment(request, pk):
    """
    Settle arrears: record one or more payments against an existing sale's
    outstanding balance. Supports a single method OR a split across
    Cash / MoMo / Card. Every payment is flagged ``is_settlement=True`` so it
    appears in the Arrears Payment Log, and the money lands in the current
    cashier's open drawer (not the original sale's drawer).
    """
    sale = get_object_or_404(Sale, pk=pk)
    user = request.user

    if user.role != 'OWNER' and not user.is_superuser and sale.location_id != user.assigned_location_id:
        messages.error(request, "You can only take payments on sales from your own location.")
        return redirect('sales:list')

    session = _open_session(user, user.assigned_location, lock=True)
    if not session:
        messages.error(request, "You must have an open register to accept payments.")
        return redirect('sales:detail', pk=pk)

    # Build the list of (method, amount) tendered — single method or a split.
    method = request.POST.get('payment_method')
    try:
        if method == 'SPLIT':
            tendered = [
                (m, parse_money(request.POST.get(field), f'{m.title()} amount'))
                for m, field in (('CASH', 'split_cash'), ('MOMO', 'split_momo'), ('CARD', 'split_card'))
            ]
        else:
            tendered = [(method, parse_money(request.POST.get('amount'), 'Amount'))]
        total_tendered, change_due = record_settlement(
            sale=sale, user=user, session=session, tendered=tendered,
        )
    except SaleError as err:
        transaction.set_rollback(True)
        messages.error(request, str(err))
        return redirect('sales:detail', pk=pk)

    if change_due > 0:
        messages.success(request, f"Payment of {total_tendered} recorded. Change returned: {change_due}.")
    else:
        messages.success(request, f"Payment of {total_tendered} recorded successfully.")
    return redirect('sales:detail', pk=pk)


@login_required
@role_required(*MANAGEMENT)
@transaction.atomic
def process_refund(request, pk):
    """
    Handle Full or Partial Refunds.

    Mirrors the sale's financial path in reverse:
      - Restocks inventory (with a StockAdjustment audit row).
      - Issues a NEGATIVE SalePayment so payment sums reconcile to net revenue.
      - Reduces Sale.amount_paid and updates status (REFUNDED / PARTIAL).
      - Debits the cashier's open RegisterSession bucket so the drawer count is right at close.
      - Decrements Customer.total_spent.
    """
    sale = get_object_or_404(Sale, pk=pk)

    if request.user.role not in ['OWNER', 'MANAGER']:
        messages.error(request, "Only Managers can process refunds.")
        return redirect('sales:detail', pk=pk)

    user = request.user

    if request.method == 'POST':
        # Refund money leaves the manager's currently-open till
        session = _open_session(user, user.assigned_location, lock=True)
        if not session:
            messages.error(request, "You must have an open register to issue a refund.")
            return redirect('sales:detail', pk=pk)

        try:
            refund_value, money_returned = record_refund(
                sale=sale, user=user, session=session,
                item_ids=request.POST.getlist('refund_items'),
                method=request.POST.get('refund_method', SalePayment.PaymentMethod.CASH),
                reason=request.POST.get('reason', 'Customer Return'),
            )
        except SaleError as err:
            transaction.set_rollback(True)
            messages.warning(request, str(err))
            return redirect('sales:detail', pk=pk)

        if money_returned == refund_value:
            messages.success(request, f"Refund processed. Amount returned: {money_returned}")
        else:
            messages.success(request, (
                f"Refund processed. Goods worth {refund_value} returned: "
                f"{refund_value - money_returned} came off the amount owed and "
                f"{money_returned} was paid back."
            ))
        return redirect('sales:detail', pk=pk)

    return render(request, 'sales/process_refund.html', {'sale': sale})


@login_required
@role_required(*SELLING_STAFF)
def delivery_management(request, pk=None):
    """
    Manage Deliveries.
    If pk is provided, edit specific delivery. Else list pending.
    """
    user = request.user
    if pk:
        # Edit/Update Delivery Status
        delivery = get_object_or_404(Delivery, pk=pk)

        # Non-owners may only manage deliveries from their own location.
        if user.role != 'OWNER' and not user.is_superuser:
            if delivery.sale.location_id != user.assigned_location_id:
                messages.error(request, "You can only manage deliveries from your own location.")
                return redirect('sales:deliveries')

        if request.method == 'POST':
            status = request.POST.get('status')
            rider_name = request.POST.get('rider_name')
            tracking_ref = request.POST.get('tracking_ref')

            if status: delivery.status = status
            if rider_name: delivery.rider_name = rider_name
            if tracking_ref: delivery.tracking_reference = tracking_ref

            if status == 'DELIVERED':
                delivery.delivered_at = timezone.now()
            elif status == 'DISPATCHED':
                delivery.dispatched_at = timezone.now()

            delivery.save()
            messages.success(request, "Delivery updated.")
            return redirect('sales:deliveries')  # Redirect to list

        return render(request, 'sales/delivery_form.html', {'delivery': delivery})

    else:
        # List View
        deliveries = Delivery.objects.filter(
            sale__location=request.user.assigned_location
        ).order_by('-created_at')
        return render(request, 'sales/delivery_list.html', {'deliveries': deliveries})


@login_required
@role_required(*SELLING_STAFF)
def refund_list(request):
    """
    Specific list for Returned/Refunded transactions.
    """
    user = request.user

    # Base Query
    if user.role == 'OWNER':
        refunds = Sale.objects.all()
    else:
        refunds = Sale.objects.filter(location=user.assigned_location)

    # Filter for Refunded Statuses
    refunds = refunds.filter(
        status__in=[Sale.Status.REFUNDED, Sale.Status.PARTIAL_REFUND]
    ).select_related('customer', 'cashier').order_by('-updated_at')

    return render(request, 'sales/refund_list.html', {'refunds': refunds})


@login_required
@role_required(*SELLING_STAFF)
def arrears_list(request):
    """
    Debtors / Arrears: customers with an outstanding balance on credit sales,
    grouped by customer with total owed and aging of the oldest debt.

    Owners see every location; everyone else is scoped to their own location.
    Only COMPLETED / PARTIAL_REFUND sales can carry a balance (fully refunded
    and cancelled sales are excluded).
    """
    user = request.user
    query = (request.GET.get('q') or '').strip()

    unpaid = Sale.objects.filter(
        status__in=[Sale.Status.COMPLETED, Sale.Status.PARTIAL_REFUND],
        amount_paid__lt=F('total_amount'),
    ).select_related('customer', 'location').order_by('created_at')

    if user.role != 'OWNER':
        unpaid = unpaid.filter(location=user.assigned_location)

    if query:
        unpaid = search_queryset(unpaid, query, [
            'customer__first_name', 'customer__last_name',
            'customer__phone_number', 'invoice_number',
        ])

    today = timezone.now().date()
    groups = {}
    for s in unpaid:
        balance = s.total_amount - s.amount_paid
        if balance <= 0:
            continue
        s.balance = balance
        g = groups.get(s.customer_id)
        if g is None:
            g = {'customer': s.customer, 'sales': [], 'total': Decimal('0.00'), 'oldest': s.created_at}
            groups[s.customer_id] = g
        g['sales'].append(s)
        g['total'] += balance
        if s.created_at < g['oldest']:
            g['oldest'] = s.created_at

    debtors = []
    for g in groups.values():
        debtors.append({
            'customer': g['customer'],
            'sales': g['sales'],
            'total': g['total'],
            'count': len(g['sales']),
            'oldest_days': (today - g['oldest'].date()).days,
        })
    debtors.sort(key=lambda d: d['total'], reverse=True)

    grand_total = sum((d['total'] for d in debtors), Decimal('0.00'))

    return render(request, 'sales/arrears_list.html', {
        'debtors': debtors,
        'grand_total': grand_total,
        'debtor_count': len(debtors),
        'query': query,
    })


@login_required
@role_required(*SELLING_STAFF)
def arrears_payment_log(request):
    """
    Ledger of every arrears/debt-settlement payment (is_settlement=True):
    when, how much, which method, which staff member, customer and invoice.
    Owners see all locations; everyone else is scoped to their own.
    """
    user = request.user
    query = (request.GET.get('q') or '').strip()
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')

    payments = SalePayment.objects.filter(is_settlement=True).select_related(
        'sale', 'sale__customer', 'sale__location', 'processed_by'
    ).order_by('-created_at')

    if user.role != 'OWNER':
        payments = payments.filter(sale__location=user.assigned_location)
    if query:
        payments = search_queryset(payments, query, [
            'sale__invoice_number', 'sale__customer__first_name',
            'sale__customer__last_name', 'sale__customer__phone_number',
        ])
    if start_date:
        payments = payments.filter(created_at__date__gte=start_date)
    if end_date:
        payments = payments.filter(created_at__date__lte=end_date)

    total_collected = payments.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

    return render(request, 'sales/arrears_log.html', {
        'payments': payments[:300],
        'total_collected': total_collected,
        'count': payments.count(),
        'filters': {'q': query, 'start_date': start_date or '', 'end_date': end_date or ''},
    })






