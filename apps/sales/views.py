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
from ..customers.models import Customer
from ..finance.models import Expense
from ..inventory.models import StockBatch, StockAdjustment
from ..location.models import Location
from ..notifications.services import SMSService
from ..products.models import Category, Product


TWO_PLACES = Decimal('0.01')


def _q(value):
    """Quantize a Decimal to 2dp (banker-safe rounding)."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


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
    active_session = RegisterSession.objects.filter(
        user=user,
        location=location,
        status=RegisterSession.Status.OPEN
    ).first()

    if not active_session:
        if request.method == 'POST':
            # Handle Opening Logic
            RegisterSession.objects.create(
                user=user,
                location=location,
                opening_balance=request.POST.get('opening_balance', 0) or 0
            )
            return redirect('sales:pos')
        return render(request, 'sales/open_register.html')

    # 2. Load Catalog Data for POS (with live stock for THIS location so the
    #    cashier can see quantity-on-hand on every product card).
    categories = Category.objects.filter(is_active=True)
    products = list(
        Product.objects.filter(is_active=True).select_related('category').order_by('name')
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

    products = Product.objects.filter(is_active=True).select_related('category')
    if query:
        products = products.filter(
            Q(name__icontains=query) |
            Q(sku__icontains=query) |
            Q(barcode__icontains=query)
        )
    products = list(products.order_by('name')[:50])

    stock_map = _location_stock_map(location, [p.id for p in products])

    results = [{
        'id': p.id,
        'name': p.name,
        'sku': p.sku,
        'barcode': p.barcode or '',
        'category_id': p.category_id,
        'retail': float(p.selling_price),
        'wholesale': float(p.wholesale_price if p.wholesale_price is not None else p.selling_price),
        'stock': int(stock_map.get(p.id, 0)),
    } for p in products]

    return JsonResponse({'results': results})


@login_required
@role_required(*SELLING_STAFF)
@require_POST
@transaction.atomic
def process_sale(request):
    try:
        data = json.loads(request.body)

        cart = data.get('cart', [])
        payments = data.get('payments', [])
        customer_id = data.get('customer_id')

        if not cart:
            return JsonResponse({'success': False, 'message': 'Cart is empty.'}, status=400)

        user = request.user
        location = user.assigned_location

        session = RegisterSession.objects.filter(
            user=user, location=location, status=RegisterSession.Status.OPEN
        ).first()

        if not session:
            return JsonResponse({'success': False, 'message': 'No active register session. Please open register.'}, status=400)

        # ------------------------------------------------------------------
        # 1. RESOLVE PRICES SERVER-SIDE (never trust client-sent amounts)
        # ------------------------------------------------------------------
        # Each line price MUST match the product's current retail or
        # wholesale price. The authoritative bill total is computed here from
        # the catalogue — not taken from the browser — so recorded revenue
        # can never be tampered with or drift from the price list.
        qty_per_product = {}
        resolved = []  # [{pid, qty, unit_price, product}]
        for entry in cart:
            try:
                pid = int(entry['id'])
                qty = int(entry['qty'])
            except (KeyError, TypeError, ValueError):
                return JsonResponse({'success': False, 'message': 'Malformed cart entry.'}, status=400)
            if qty <= 0:
                continue

            try:
                product = Product.objects.get(id=pid, is_active=True)
            except Product.DoesNotExist:
                return JsonResponse({'success': False, 'message': f'Product {pid} not found.'}, status=400)

            retail = _q(product.selling_price)
            wholesale = _q(product.wholesale_price) if product.wholesale_price is not None else retail
            client_price = _q(str(entry.get('price', 0)))
            if client_price == retail:
                unit_price = retail
            elif client_price == wholesale:
                unit_price = wholesale
            else:
                return JsonResponse({
                    'success': False,
                    'message': f'Price for {product.name} is out of date. Please refresh the POS and try again.',
                }, status=400)

            qty_per_product[pid] = qty_per_product.get(pid, 0) + qty
            resolved.append({'pid': pid, 'qty': qty, 'unit_price': unit_price, 'product': product})

        if not resolved:
            return JsonResponse({'success': False, 'message': 'Cart is empty.'}, status=400)

        # Authoritative total (VAT-inclusive sticker prices x quantities).
        total_amount = _q(sum((r['unit_price'] * r['qty'] for r in resolved), Decimal('0.00')))

        # ------------------------------------------------------------------
        # 2. STOCK VALIDATION (with row-level lock to prevent oversell races)
        # ------------------------------------------------------------------
        # Lock batches per product and check availability up-front.
        # Postgres honors select_for_update; SQLite ignores it harmlessly.
        batches_by_product = {}
        for pid, qty_needed in qty_per_product.items():
            batches = list(
                StockBatch.objects.select_for_update()
                .filter(product_id=pid, location=location, quantity__gt=0)
                .order_by('expiry_date', 'received_date')
            )
            available = sum(b.quantity for b in batches)
            if available < qty_needed:
                name = next(r['product'].name for r in resolved if r['pid'] == pid)
                return JsonResponse({
                    'success': False,
                    'message': f'Insufficient stock for {name}. Requested {qty_needed}, available {available}.'
                }, status=400)
            batches_by_product[pid] = batches

        # ------------------------------------------------------------------
        # 3. CREATE SALE & DEDUCT STOCK (FEFO)
        # ------------------------------------------------------------------
        sale = Sale.objects.create(
            location=location,
            cashier=user,
            register_session=session,
            total_amount=total_amount,
            status=Sale.Status.COMPLETED,
            amount_paid=0,
            customer_id=customer_id
        )

        sale_subtotal = Decimal('0.00')   # excl. tax
        sale_tax_total = Decimal('0.00')
        tax_by_rate = {}                   # Decimal rate -> Decimal tax amount

        for r in resolved:
            pid = r['pid']
            unit_price = r['unit_price']
            qty_remaining = r['qty']
            product = r['product']

            # FEFO allocation across the locked batch list (shared across cart entries for the same product)
            batches = batches_by_product[pid]
            for batch in batches:
                if qty_remaining <= 0:
                    break
                if batch.quantity <= 0:
                    continue
                take = min(batch.quantity, qty_remaining)

                SaleItem.objects.create(
                    sale=sale,
                    product=product,
                    source_batch=batch,
                    quantity=take,
                    unit_price=unit_price,
                    unit_cost=batch.cost_price,
                    total_price=_q(unit_price * take),
                )

                batch.quantity -= take
                batch.save()
                qty_remaining -= take

            if qty_remaining > 0:
                # Should never happen — we validated availability above with row locks.
                raise RuntimeError(
                    f"Stock validation passed but ran short during allocation for {product.name}."
                )

            # Tax / subtotal split (treat unit_price as VAT-inclusive)
            line_total = _q(unit_price * Decimal(r['qty']))
            rate = Decimal(product.tax_rate or 0)
            if rate > 0:
                line_tax = _q(line_total * rate / (Decimal('100') + rate))
                line_excl = _q(line_total - line_tax)
                tax_by_rate[rate] = tax_by_rate.get(rate, Decimal('0.00')) + line_tax
                sale_tax_total += line_tax
            else:
                line_excl = line_total
            sale_subtotal += line_excl

        sale.subtotal = _q(sale_subtotal)
        sale.total_tax = _q(sale_tax_total)

        # Persist tax breakdown rows (one per distinct rate)
        for rate, amt in tax_by_rate.items():
            if amt > 0:
                SaleTax.objects.create(
                    sale=sale,
                    tax_name='VAT',
                    tax_rate=rate,
                    tax_amount=_q(amt),
                )

        # ------------------------------------------------------------------
        # 4. PAYMENTS + CHANGE (balanced via negative SalePayment)
        # ------------------------------------------------------------------
        total_paid = Decimal('0.00')
        total_cash_tendered = Decimal('0.00')
        valid_methods = {m.value for m in SalePayment.PaymentMethod}

        for pay in payments:
            amount = _q(str(pay.get('amount', 0)))
            method = pay.get('method')
            if amount <= 0:
                continue
            if method not in valid_methods:
                return JsonResponse({'success': False, 'message': f'Invalid payment method: {method}.'}, status=400)

            SalePayment.objects.create(
                sale=sale, payment_method=method, amount=amount, processed_by=user
            )
            total_paid += amount

            if method == 'CASH':
                total_cash_tendered += amount
            elif method == 'MOMO':
                session.total_momo_sales += amount
            elif method == 'CARD':
                session.total_card_sales += amount

        change_due = max(Decimal('0.00'), total_paid - total_amount)

        if change_due > 0:
            SalePayment.objects.create(
                sale=sale,
                payment_method=SalePayment.PaymentMethod.CASH,
                amount=-change_due,
                reference_id="CHANGE GIVEN",
                processed_by=user
            )
            total_paid -= change_due

        sale.amount_paid = total_paid
        sale.change_due = change_due
        sale.save()

        # Drawer reflects net cash retained
        net_cash_added = total_cash_tendered - change_due
        session.total_cash_sales += net_cash_added
        session.save()

        # ------------------------------------------------------------------
        # 4. CUSTOMER STATS + SMS
        # ------------------------------------------------------------------
        if customer_id:
            try:
                customer = Customer.objects.get(id=customer_id)
                customer.total_spent += sale.total_amount
                customer.total_visits += 1
                customer.last_visit_date = timezone.now()
                customer.save()

                if customer.accepts_marketing_sms and SMSService:
                    try:
                        SMSService.send_receipt(sale)
                    except Exception as sms_error:
                        print(f"SMS Error: {sms_error}")
            except Customer.DoesNotExist:
                pass

        return JsonResponse({
            'success': True,
            'invoice_number': sale.invoice_number,
            'sale_id': sale.id,
            'total_amount': float(sale.total_amount),
            'amount_paid': float(sale.amount_paid),
            'change_due': float(change_due),
        })

    except Exception as e:
        # transaction.atomic rolls back automatically on exception
        return JsonResponse({'success': False, 'message': str(e)}, status=400)


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
        sales = sales.filter(
            Q(invoice_number__icontains=query) |
            Q(customer__phone_number__icontains=query) |
            Q(customer__first_name__icontains=query)
        )

    # 2. Status Filter (Enhanced for Debt)
    status = request.GET.get('status')
    if status:
        if status == 'DEBT':
            # Find sales where amount paid is less than total amount
            sales = sales.filter(amount_paid__lt=F('total_amount'))
        else:
            sales = sales.filter(status=status)

    # 3. Date Range
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    if start_date:
        sales = sales.filter(created_at__date__gte=start_date)
    if end_date:
        sales = sales.filter(created_at__date__lte=end_date)

    # Context for Owner Location Filter
    locations = []
    if user.role == 'OWNER':
        locations = Location.objects.filter(is_active=True)

    return render(request, 'sales/sale_list.html', {
        'sales': sales,
        'filters': {
            'q': query,
            'status': status,
            'start_date': start_date,
            'end_date': end_date,
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
    session = RegisterSession.objects.filter(
        user=user,
        location=location,
        status=RegisterSession.Status.OPEN
    ).first()

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
        actual_cash_str = request.POST.get('actual_cash', '0')
        notes = request.POST.get('notes', '')

        try:
            actual_cash = Decimal(actual_cash_str)
        except:
            actual_cash = Decimal('0.00')

        # Update Session
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

    session = RegisterSession.objects.filter(
        user=user,
        location=user.assigned_location,
        status=RegisterSession.Status.OPEN
    ).first()
    if not session:
        messages.error(request, "You must have an open register to accept payments.")
        return redirect('sales:detail', pk=pk)

    # Build the list of (method, amount) tendered — single method or a split.
    valid_methods = {pm.value for pm in SalePayment.PaymentMethod}
    method = request.POST.get('payment_method')
    tendered = []
    if method == 'SPLIT':
        for m, field in (('CASH', 'split_cash'), ('MOMO', 'split_momo'), ('CARD', 'split_card')):
            amt = _q(request.POST.get(field) or 0)
            if amt > 0:
                tendered.append((m, amt))
    elif method in valid_methods:
        amt = _q(request.POST.get('amount') or 0)
        if amt > 0:
            tendered.append((method, amt))

    total_tendered = sum((a for _, a in tendered), Decimal('0.00'))
    if total_tendered <= 0:
        messages.error(request, "Enter a valid payment amount.")
        return redirect('sales:detail', pk=pk)

    # Overpayment above the outstanding balance is returned as cash change.
    balance = max(Decimal('0.00'), sale.total_amount - sale.amount_paid)
    change_due = max(Decimal('0.00'), total_tendered - balance)
    net_applied = total_tendered - change_due

    # Record each tendered amount as a settlement payment + bump the drawer bucket.
    for m, a in tendered:
        SalePayment.objects.create(
            sale=sale, payment_method=m, amount=a, processed_by=user,
            is_settlement=True, reference_id="DEBT SETTLEMENT",
        )
        if m == 'CASH':
            session.total_cash_sales += a
        elif m == 'MOMO':
            session.total_momo_sales += a
        elif m == 'CARD':
            session.total_card_sales += a

    # Change is always handed back in physical cash.
    if change_due > 0:
        SalePayment.objects.create(
            sale=sale, payment_method=SalePayment.PaymentMethod.CASH,
            amount=-change_due, reference_id="CHANGE GIVEN", processed_by=user,
        )
        session.total_cash_sales -= change_due

    sale.amount_paid += net_applied
    sale.change_due += change_due
    # Only a draft (PENDING) sale graduates to COMPLETED here; credit sales are
    # already COMPLETED and partially-refunded sales keep their status.
    if sale.status == Sale.Status.PENDING_PAYMENT and sale.amount_paid >= sale.total_amount:
        sale.status = Sale.Status.COMPLETED
    sale.save()
    session.save()

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
        # Refund cash leaves the manager's currently-open till
        session = RegisterSession.objects.filter(
            user=user,
            location=user.assigned_location,
            status=RegisterSession.Status.OPEN,
        ).first()
        if not session:
            messages.error(request, "You must have an open register to issue a refund.")
            return redirect('sales:detail', pk=pk)

        refund_reason = request.POST.get('reason', 'Customer Return')
        refund_method = request.POST.get('refund_method', SalePayment.PaymentMethod.CASH)
        items_to_refund = request.POST.getlist('refund_items')

        total_refund_amount = Decimal('0.00')
        refunded_count = 0

        for item_id in items_to_refund:
            sale_item = get_object_or_404(SaleItem, id=item_id, sale=sale)
            if sale_item.is_refunded:
                continue

            # 1. Mark item refunded
            sale_item.is_refunded = True
            sale_item.save()
            refunded_count += 1

            # 2. Restock and audit (if we know which batch it came from)
            if sale_item.source_batch:
                # Lock the batch row to avoid races with concurrent sales
                batch = StockBatch.objects.select_for_update().get(pk=sale_item.source_batch_id)
                batch.quantity += sale_item.quantity
                batch.save()

                StockAdjustment.objects.create(
                    location=sale.location,
                    batch=batch,
                    adjusted_quantity=sale_item.quantity,
                    reason='RETURN',
                    notes=f"Refund for Invoice #{sale.invoice_number} ({refund_reason})",
                    performed_by=user,
                )

            total_refund_amount += sale_item.total_price

        if refunded_count == 0:
            messages.warning(request, "No items selected for refund.")
            return redirect('sales:detail', pk=pk)

        total_refund_amount = _q(total_refund_amount)

        # 3. Negative SalePayment so SUM(SalePayment.amount) reconciles to net revenue
        SalePayment.objects.create(
            sale=sale,
            payment_method=refund_method,
            amount=-total_refund_amount,
            reference_id=f"REFUND - {refund_reason}"[:100],
            processed_by=user,
        )

        # 4. Reduce sale.amount_paid AND total_amount by the refunded value.
        # Reducing total_amount too keeps balance_remaining (= total_amount -
        # amount_paid) correct after a partial refund on a credit/debt sale —
        # otherwise the customer would appear to still owe for returned goods.
        sale.amount_paid = max(Decimal('0.00'), _q(sale.amount_paid - total_refund_amount))
        sale.total_amount = max(Decimal('0.00'), _q(sale.total_amount - total_refund_amount))

        # 5. Sale status: REFUNDED if no items remain unrefunded, else PARTIAL
        all_refunded = not sale.items.filter(is_refunded=False).exists()
        sale.status = Sale.Status.REFUNDED if all_refunded else Sale.Status.PARTIAL_REFUND
        sale.save()

        # 6. Drawer adjustment — refund cash leaves the till
        if refund_method == SalePayment.PaymentMethod.CASH:
            session.total_cash_sales -= total_refund_amount
        elif refund_method == SalePayment.PaymentMethod.MOMO:
            session.total_momo_sales -= total_refund_amount
        elif refund_method == SalePayment.PaymentMethod.CARD:
            session.total_card_sales -= total_refund_amount
        session.save()

        # 7. Customer stats — decrement spend, leave visit count alone (the visit happened)
        if sale.customer_id:
            try:
                customer = Customer.objects.get(id=sale.customer_id)
                customer.total_spent = max(Decimal('0.00'), customer.total_spent - total_refund_amount)
                customer.save()
            except Customer.DoesNotExist:
                pass

        messages.success(request, f"Refund processed. Amount returned: {total_refund_amount}")
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
        unpaid = unpaid.filter(
            Q(customer__first_name__icontains=query) |
            Q(customer__last_name__icontains=query) |
            Q(customer__phone_number__icontains=query) |
            Q(invoice_number__icontains=query)
        )

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
        payments = payments.filter(
            Q(sale__invoice_number__icontains=query) |
            Q(sale__customer__first_name__icontains=query) |
            Q(sale__customer__last_name__icontains=query) |
            Q(sale__customer__phone_number__icontains=query)
        )
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






