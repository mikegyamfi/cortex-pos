import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, F
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Sale, SaleItem, RegisterSession, SalePayment, Delivery
from ..customers.models import Customer
from ..inventory.models import StockBatch, StockAdjustment
from ..location.models import Location
from ..notifications.services import SMSService
from ..products.models import Category, Product


@login_required
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
                opening_balance=request.POST.get('opening_balance', 0)
            )
            return redirect('sales:pos')
        return render(request, 'sales/open_register.html')

    # 2. Load Catalog Data for POS
    # We load active categories and products to populate the initial grid
    categories = Category.objects.filter(is_active=True)
    products = Product.objects.filter(is_active=True).select_related('category')

    return render(request, 'sales/pos.html', {
        'session': active_session,
        'location': location,
        'categories': categories,
        'products': products[:50]
    })


@login_required
@require_POST
@transaction.atomic
def process_sale(request):
    try:
        data = json.loads(request.body)

        cart = data.get('cart', [])
        payments = data.get('payments', [])
        total_amount = Decimal(str(data.get('total_amount', 0)))
        customer_id = data.get('customer_id')

        user = request.user
        location = user.assigned_location

        session = RegisterSession.objects.filter(
            user=user, location=location, status=RegisterSession.Status.OPEN
        ).first()

        if not session:
            return JsonResponse({'success': False, 'message': 'No active register session. Please open register.'})

        sale = Sale.objects.create(
            location=location,
            cashier=user,
            register_session=session,
            total_amount=total_amount,
            status=Sale.Status.COMPLETED,
            amount_paid=0,
            customer_id=customer_id
        )

        # Process Cart Items (Stock Deduction via FEFO)
        for item in cart:
            product_id = item['id']
            qty_sold = int(item['qty'])
            unit_price = Decimal(str(item['price']))

            product = Product.objects.get(id=product_id)

            batches = StockBatch.objects.filter(
                product=product, location=location, quantity__gt=0
            ).order_by('expiry_date')

            qty_needed = qty_sold

            for batch in batches:
                if qty_needed <= 0: break
                take = min(batch.quantity, qty_needed)

                SaleItem.objects.create(
                    sale=sale, product=product, source_batch=batch,
                    quantity=take, unit_price=unit_price, unit_cost=batch.cost_price,
                    total_price=take * unit_price
                )

                batch.quantity -= take
                batch.save()
                qty_needed -= take

            if qty_needed > 0:
                SaleItem.objects.create(
                    sale=sale, product=product, source_batch=None,
                    quantity=qty_needed, unit_price=unit_price, unit_cost=product.cost_price,
                    total_price=qty_needed * unit_price
                )

        # Process Payments
        total_paid = Decimal('0.00')
        total_cash_tendered = Decimal('0.00')

        for pay in payments:
            amount = Decimal(str(pay['amount']))
            method = pay['method']

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

        # Calculate Change
        change_due = max(Decimal('0.00'), total_paid - total_amount)

        # --- THE FIX: BALANCING OVERPAYMENTS ---
        # If the customer overpaid, we give them cash back.
        # We must log this as a negative payment so our revenue matches the actual sale total.
        if change_due > 0:
            SalePayment.objects.create(
                sale=sale,
                payment_method='CASH',  # Change is always physical cash
                amount=-change_due,
                reference_id="CHANGE GIVEN",
                processed_by=user
            )
            # Adjust the mathematical total_paid down to perfectly match the bill
            total_paid -= change_due

        # Save Final Sale Financials
        sale.amount_paid = total_paid
        sale.change_due = change_due
        sale.save()

        # Update the Cashier's Drawer (Subtracts the change we handed out)
        net_cash_added = total_cash_tendered - change_due
        session.total_cash_sales += net_cash_added
        session.save()

        # Update Customer Stats
        if customer_id:
            try:
                customer = Customer.objects.get(id=customer_id)
                customer.total_spent += sale.total_amount
                customer.total_visits += 1
                customer.last_visit_date = timezone.now()
                customer.save()

                # Send SMS Receipt
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
            'sale_id': sale.id
        })

    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)


@login_required
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
def sale_detail(request, pk):
    """
    View Receipt / Sale Details.
    """
    sale = get_object_or_404(Sale, pk=pk)
    return render(request, 'sales/sale_detail.html', {'sale': sale})


@login_required
def session_list(request):
    """
    List of cashier shifts (for closing/reconciling).
    """
    sessions = RegisterSession.objects.filter(location=request.user.assigned_location).order_by('-created_at')
    return render(request, 'sales/session_list.html', {'sessions': sessions})


@login_required
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

    # Calculate Expected Totals
    # Opening Balance + Cash Sales
    expected_cash = session.opening_balance + session.total_cash_sales

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
        'expected_cash': expected_cash
    })


@login_required
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
@require_POST
@transaction.atomic
def add_payment(request, pk):
    """
    Settle Debt: Add a payment to an existing sale.
    """
    sale = get_object_or_404(Sale, pk=pk)
    user = request.user

    # 1. Get Active Session (Money goes to CURRENT drawer, not original sale drawer)
    session = RegisterSession.objects.filter(
        user=user,
        location=user.assigned_location,
        status=RegisterSession.Status.OPEN
    ).first()

    if not session:
        messages.error(request, "You must have an open register to accept payments.")
        return redirect('sales:detail', pk=pk)

    # 2. Process Form Data
    amount = Decimal(request.POST.get('amount', 0))
    method = request.POST.get('payment_method')

    if amount <= 0:
        messages.error(request, "Invalid amount.")
        return redirect('sales:detail', pk=pk)

    # Check if overpaying
    balance = sale.total_amount - sale.amount_paid
    if amount > balance:
        # Optional: Allow overpayment as change/tip? For now, stick to balance.
        # messages.warning(request, "Amount exceeds balance. adjusted.")
        # amount = balance
        pass

        # 3. Create Payment Record
    SalePayment.objects.create(
        sale=sale,
        payment_method=method,
        amount=amount,
        processed_by=user
    )

    # 4. Update Sale Totals
    sale.amount_paid += amount
    # Recalculate Change Due (if they paid off everything and exceeded)
    # Usually for debt settlement, change is handled physically, record exact payment.
    sale.change_due = max(Decimal('0.00'), sale.amount_paid - sale.total_amount)

    # Update Status if fully paid
    if sale.amount_paid >= sale.total_amount:
        sale.status = Sale.Status.COMPLETED
        # If it was pending/partial, mark completed?
        # Usually we keep status as COMPLETED but maybe add a 'paid_in_full' flag logic
        pass

    sale.save()

    # 5. Update Current Register Session
    if method == 'CASH':
        session.total_cash_sales += amount
    elif method == 'MOMO':
        session.total_momo_sales += amount
    elif method == 'CARD':
        session.total_card_sales += amount
    session.save()

    messages.success(request, f"Payment of {amount} recorded successfully.")
    return redirect('sales:detail', pk=pk)


@login_required
@transaction.atomic
def process_refund(request, pk):
    """
    Handle Full or Partial Refunds.
    Restocks inventory and updates sales records.
    """
    sale = get_object_or_404(Sale, pk=pk)

    # Permission Check
    if request.user.role not in ['OWNER', 'MANAGER']:
        messages.error(request, "Only Managers can process refunds.")
        return redirect('sales:detail', pk=pk)

    if request.method == 'POST':
        refund_reason = request.POST.get('reason', 'Customer Return')
        items_to_refund = request.POST.getlist('refund_items')  # List of SaleItem IDs

        total_refund_amount = Decimal('0.00')

        for item_id in items_to_refund:
            sale_item = get_object_or_404(SaleItem, id=item_id, sale=sale)

            if not sale_item.is_refunded:
                # 1. Update Item Status
                sale_item.is_refunded = True
                sale_item.save()

                # 2. Restore Stock (if batch exists)
                if sale_item.source_batch:
                    sale_item.source_batch.quantity += sale_item.quantity
                    sale_item.source_batch.save()

                    # Log Adjustment
                    StockAdjustment.objects.create(
                        location=sale.location,
                        batch=sale_item.source_batch,
                        adjusted_quantity=sale_item.quantity,
                        reason='RETURN',  # Ensure 'RETURN' is in Reason choices or use closest match
                        notes=f"Refund for Invoice #{sale.invoice_number}",
                        performed_by=request.user
                    )

                total_refund_amount += sale_item.total_price

        # 3. Update Sale Status
        if total_refund_amount > 0:
            # If all items refunded, mark sale as REFUNDED, else PARTIAL
            all_refunded = not sale.items.filter(is_refunded=False).exists()
            sale.status = Sale.Status.REFUNDED if all_refunded else Sale.Status.PARTIAL_REFUND
            sale.save()

            messages.success(request, f"Refund processed. Amount: {total_refund_amount}")
        else:
            messages.warning(request, "No items selected for refund.")

        return redirect('sales:detail', pk=pk)

    return render(request, 'sales/process_refund.html', {'sale': sale})


@login_required
def delivery_management(request, pk=None):
    """
    Manage Deliveries.
    If pk is provided, edit specific delivery. Else list pending.
    """
    if pk:
        # Edit/Update Delivery Status
        delivery = get_object_or_404(Delivery, pk=pk)
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






