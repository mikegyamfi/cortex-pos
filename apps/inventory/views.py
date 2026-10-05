from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.forms import formset_factory
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q, Sum
from urllib.parse import urlencode
from decimal import Decimal

from .models import StockBatch, StockTransfer, StockTransferItem, StockAdjustment
from .forms import StockReceiveForm, StockTransferForm, StockTransferItemForm, StockAdjustmentForm
from apps.core.decorators import role_required, INVENTORY_STAFF
from apps.core.search import search_queryset, text_matches
from apps.location.models import Location
from apps.products.models import Product
from ..sales.models import SaleItem


@login_required
@role_required(*INVENTORY_STAFF)
def inventory_dashboard(request):
    """
    Inventory Dashboard.
    - Owners: Can switch view to any location.
    - Staff: Locked to their assigned location.

    Supports searching for a product (name / SKU / barcode / batch number) and
    switching the table between stock on hand, out-of-stock and low-stock
    products via ?stock=out / ?stock=low.
    """
    user = request.user
    selected_location_id = request.GET.get('location')
    query = (request.GET.get('q') or '').strip()
    stock_view = request.GET.get('stock') or ''
    locations = Location.objects.filter(is_active=True)

    # 1. Determine Scope
    if user.role == 'OWNER':
        if selected_location_id:
            location_scope = locations.filter(id=selected_location_id).first()
            batches = StockBatch.objects.filter(location=location_scope, quantity__gt=0)
        else:
            location_scope = None  # Represents "All Locations"
            batches = StockBatch.objects.filter(quantity__gt=0)
    else:
        location_scope = user.assigned_location
        batches = StockBatch.objects.filter(location=location_scope, quantity__gt=0)

    batches = batches.select_related('product', 'location')

    # 2. Stock position per product (drives the summary cards and the
    #    out-of-stock / low-stock listings).
    products = Product.objects.filter(is_active=True).select_related('location')
    if location_scope:
        products = products.filter(location=location_scope)

    on_hand = {
        row['product_id']: row['total'] or 0
        for row in batches.values('product_id').annotate(total=Sum('quantity'))
    }

    out_of_stock, low_stock = [], []
    for product in products:
        product.on_hand = on_hand.get(product.id, 0)
        if product.on_hand <= 0:
            out_of_stock.append(product)
        elif product.on_hand <= product.low_stock_threshold:
            low_stock.append(product)

    # The summary cards always show the whole shop, so the counts don't move
    # around while someone is searching.
    in_stock_count, out_of_stock_count, low_stock_count = len(on_hand), len(out_of_stock), len(low_stock)
    total_value = sum(b.quantity * b.cost_price for b in batches)

    # Expiry warning covers the whole shop too (next 30 days).
    today = timezone.now().date()
    thirty_days = today + timedelta(days=30)
    expiring_soon = [b for b in batches if b.expiry_date and b.expiry_date <= thirty_days]

    # 3. Search — narrows the table only.
    if query:
        batches = search_queryset(batches, query, [
            'product__name', 'product__sku', 'product__barcode', 'batch_number',
        ])

        def matches(product):
            return text_matches(query, product.name, product.sku, product.barcode, fuzzy=True)

        out_of_stock = [p for p in out_of_stock if matches(p)]
        low_stock = [p for p in low_stock if matches(p)]

    # The table shows stock on hand by default, or the product list behind
    # whichever summary card was clicked.
    product_rows = None
    if stock_view == 'out':
        product_rows = out_of_stock
    elif stock_view == 'low':
        product_rows = low_stock

    # Stock on hand is grouped PER PRODUCT, not per batch. Every receipt creates
    # a new StockBatch, so an ungrouped list shows "5" and "20" on separate rows
    # and looks like the stock never went up, while the POS (which sums batches)
    # correctly shows 25.
    stock_rows = []
    if product_rows is None:
        grouped = {}
        for batch in batches:
            key = (batch.product_id, batch.location_id)
            row = grouped.get(key)
            if row is None:
                row = grouped[key] = {
                    'product': batch.product,
                    'location': batch.location,
                    'quantity': 0,
                    'value': Decimal('0.00'),
                    'batches': [],
                    'expiry_date': None,
                }
            row['quantity'] += batch.quantity
            row['value'] += batch.quantity * batch.cost_price
            row['batches'].append(batch)
            if batch.expiry_date and (row['expiry_date'] is None or batch.expiry_date < row['expiry_date']):
                row['expiry_date'] = batch.expiry_date
        stock_rows = sorted(grouped.values(), key=lambda r: r['product'].name.lower())

    # Query strings for the summary-card links (keep location + search) and for
    # the "clear search" link (keep location + which card is active).
    keep = {}
    if selected_location_id:
        keep['location'] = selected_location_id
    base_query = urlencode(dict(keep, **({'q': query} if query else {})))
    clear_query = urlencode(dict(keep, **({'stock': stock_view} if stock_view else {})))

    context = {
        'location': location_scope,  # If None, template handles "All Locations"
        'locations': locations,  # For the dropdown
        'batches': batches,
        'expiring_soon': expiring_soon,
        'total_value': total_value,
        'selected_location_id': int(selected_location_id) if selected_location_id else None,
        'query': query,
        'base_query': base_query,
        'clear_query': clear_query,
        'stock_view': stock_view,
        'product_rows': product_rows,
        'stock_rows': stock_rows,
        'in_stock_count': in_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'low_stock_count': low_stock_count,
    }
    return render(request, 'inventory/dashboard.html', context)


def _pull_stock_from(product, source_location, quantity):
    """
    Take `quantity` units of `product` out of `source_location` using FEFO.

    Products are shop-scoped, so the source usually holds its own copy of the
    item — we fall back to matching on SKU/barcode at that location.
    Returns the number of units actually deducted (may be less than asked for).
    """
    batches = StockBatch.objects.filter(location=source_location, quantity__gt=0)
    matching = batches.filter(product=product)

    if not matching.exists():
        lookup = Q(sku=product.sku)
        if product.barcode:
            lookup |= Q(barcode=product.barcode)
        twin = Product.objects.filter(location=source_location).filter(lookup).exclude(pk=product.pk).first()
        if twin:
            matching = batches.filter(product=twin)

    taken = 0
    for batch in matching.order_by('expiry_date', 'received_date').select_for_update():
        if taken >= quantity:
            break
        take = min(batch.quantity, quantity - taken)
        batch.quantity -= take
        batch.save(update_fields=['quantity', 'updated_at'])
        taken += take
    return taken


@login_required
@role_required(*INVENTORY_STAFF)
@transaction.atomic
def receive_stock(request):
    """
    Goods Received Note (GRN) View — the single-step receiving flow.

    Pick a product, say how many, and (optionally) where it came from:
    - Source left blank  -> supplier / external delivery, stock is just added.
    - Source chosen      -> stock is deducted from that location and a completed
                            StockTransfer is logged for the audit trail.
    - Owners: Can receive into any location.
    - Staff: Locked to receiving into their assigned location.
    """
    user = request.user

    if request.method == 'POST':
        # Pass user to form to unlock fields if Owner
        form = StockReceiveForm(user, request.POST)
        if form.is_valid():
            batch = form.save(commit=False)

            # If not owner, force location logic (Security)
            if user.role != 'OWNER':
                batch.location = user.assigned_location
            # If owner, batch.location is already set from cleaned_data via form

            source = form.cleaned_data.get('source_location')
            batch.save()

            if source:
                pulled = _pull_stock_from(batch.product, source, batch.quantity)

                # Log the movement so it shows up in Transfers & Movement.
                transfer = StockTransfer.objects.create(
                    source_location=source,
                    destination_location=batch.location,
                    status=StockTransfer.Status.RECEIVED,
                    requested_by=user,
                    approved_by=user,
                    received_by=user,
                    notes=f"Direct receipt at {batch.location.name} (collected and confirmed in one step).",
                )
                StockTransferItem.objects.create(
                    transfer=transfer,
                    product=batch.product,
                    quantity_requested=batch.quantity,
                    quantity_sent=pulled,
                    quantity_received=batch.quantity,
                )
                if not batch.batch_number:
                    batch.batch_number = f"TRF-{transfer.reference_number}"
                    batch.save(update_fields=['batch_number', 'updated_at'])

                if pulled < batch.quantity:
                    messages.warning(
                        request,
                        f"{source.name} only had {pulled} of {batch.quantity} x {batch.product.name} on record — "
                        f"the remaining {batch.quantity - pulled} were added as new stock. "
                        f"Check {source.name}'s stock levels."
                    )

            origin = f" from {source.name}" if source else ""
            messages.success(
                request,
                f"Received {batch.quantity} x {batch.product.name}{origin} into {batch.location.name}."
            )
            return redirect('inventory:dashboard')
    else:
        form = StockReceiveForm(user)

    return render(request, 'inventory/receive_stock.html', {'form': form})


@login_required
@role_required(*INVENTORY_STAFF)
def batch_detail(request, pk):
    """
    Complete Audit History for a specific Stock Batch.
    Shows:
    1. Origin (Supplier/Transfer)
    2. Adjustments (Damages/Corrections)
    3. Sales (Where did the stock go?)
    """
    batch = get_object_or_404(StockBatch, pk=pk)

    # 1. Adjustments History
    adjustments = batch.stockadjustment_set.select_related('performed_by').order_by('-created_at')

    # 2. Sales History (Items sold from this specific batch)
    sales_items = SaleItem.objects.filter(source_batch=batch).select_related(
        'sale', 'sale__cashier'
    ).order_by('-sale__created_at')

    # Statistics
    total_sold = sales_items.aggregate(Sum('quantity'))['quantity__sum'] or 0
    total_adjusted = adjustments.aggregate(Sum('adjusted_quantity'))['adjusted_quantity__sum'] or 0

    # Calculate initial quantity (Current + Sold - Adjustments)
    # Note: Logic depends on how you want to present "Initial".
    # Since we track current quantity, Initial = Current + Sold + (-Adjustments) roughly.
    # But batch.quantity is the source of truth for "Now".

    context = {
        'batch': batch,
        'adjustments': adjustments,
        'sales_items': sales_items,
        'total_sold': total_sold,
        'total_adjusted': total_adjusted,
    }
    if request.GET.get('partial'):
        return render(request, 'inventory/partials/_batch_quickview.html', context)
    return render(request, 'inventory/batch_detail.html', context)


@login_required
@role_required(*INVENTORY_STAFF)
@transaction.atomic
def create_transfer(request):
    """
    Initiate Stock Movement.
    - Owners: Can select Source AND Destination.
    - Staff: Source is locked to their location (PUSH model).
    """
    user = request.user
    ItemFormSet = formset_factory(StockTransferItemForm, extra=1)

    if request.method == 'POST':
        transfer_form = StockTransferForm(user, request.POST)
        item_formset = ItemFormSet(request.POST)

        if transfer_form.is_valid() and item_formset.is_valid():
            transfer = transfer_form.save(commit=False)
            transfer.requested_by = user
            transfer.status = StockTransfer.Status.PENDING_APPROVAL

            # Logic for Source Location
            if user.role != 'OWNER':
                # Staff logic: Sending from their location
                transfer.source_location = user.assigned_location

            transfer.save()

            # Save items
            for form in item_formset:
                if form.cleaned_data:
                    StockTransferItem.objects.create(
                        transfer=transfer,
                        product=form.cleaned_data['product'],
                        quantity_requested=form.cleaned_data['quantity_requested']
                    )

            messages.success(request, f"Transfer {transfer.reference_number} created successfully.")
            return redirect('inventory:transfer_list')
    else:
        transfer_form = StockTransferForm(user)
        item_formset = ItemFormSet()

    return render(request, 'inventory/create_transfer.html', {
        'transfer_form': transfer_form,
        'item_formset': item_formset
    })


@login_required
@role_required(*INVENTORY_STAFF)
def transfer_list(request):
    """
    History of Stock Movements.
    - Owners: See ALL transfers.
    - Staff: See transfers involving their location (Source or Dest).
    """
    user = request.user
    if user.role == 'OWNER':
        transfers = StockTransfer.objects.all().select_related('source_location', 'destination_location').order_by(
            '-created_at')
    else:
        loc = user.assigned_location
        transfers = StockTransfer.objects.filter(
            Q(source_location=loc) | Q(destination_location=loc)
        ).select_related('source_location', 'destination_location').order_by('-created_at')

    return render(request, 'inventory/transfer_list.html', {'transfers': transfers})


@login_required
@role_required(*INVENTORY_STAFF)
def transfer_detail(request, pk):
    """
    Read-Only view for any transfer (Completed, Pending, etc.)
    """
    transfer = get_object_or_404(StockTransfer, pk=pk)

    processed_items = []

    # If it's pending, we can show predicted allocations (what system suggests)
    if transfer.status == StockTransfer.Status.PENDING_APPROVAL:
        for item in transfer.items.all():
            available_batches = StockBatch.objects.filter(
                product=item.product,
                location=transfer.source_location,
                quantity__gt=0
            ).order_by('expiry_date')

            needed = item.quantity_requested
            allocated_batches = []

            for batch in available_batches:
                if needed <= 0:
                    break
                take_qty = min(batch.quantity, needed)
                allocated_batches.append({'batch_obj': batch, 'take_qty': take_qty})
                needed -= take_qty

            processed_items.append({
                'item': item,
                'allocated': allocated_batches,
                'fulfilled_qty': item.quantity_requested - needed,
                'shortfall': needed > 0
            })
    else:
        # If completed/transit, just show the items without allocation breakdown (unless we logged it)
        for item in transfer.items.select_related('product').all():
            processed_items.append({
                'item': item,
                'allocated': [],
                'fulfilled_qty': item.quantity_sent,
                'shortfall': False
            })

    context = {
        'transfer': transfer,
        'processed_items': processed_items,
        'read_only': False  # We will use this flag in the template
    }
    if request.GET.get('partial'):
        return render(request, 'inventory/partials/_transfer_quickview.html', context)
    # Reuse the same template but we won't show the 'Process' buttons
    return render(request, 'inventory/transfer_detail.html', context)


@login_required
@role_required(*INVENTORY_STAFF)
@transaction.atomic
def process_transfer(request, pk):
    """
    WAREHOUSE/SOURCE ACTION: Dispatch Logic.
    1. Reviews Request.
    2. Allocates Batches (FEFO Logic).
    3. DEDUCTS from Source.
    4. Sets status to IN_TRANSIT.
    """
    transfer = get_object_or_404(StockTransfer, pk=pk)
    user = request.user

    # Security: Ensure user is at Source Location OR is Owner
    if user.role != 'OWNER' and user.assigned_location != transfer.source_location:
        messages.error(request, "You can only process transfers from your own location.")
        return redirect('inventory:transfer_list')

    if transfer.status != StockTransfer.Status.PENDING_APPROVAL:
        messages.error(request, "Transfer already processed.")
        return redirect('inventory:transfer_list')

    # FEFO (First Expire First Out) Calculation
    processed_items = []
    for item in transfer.items.all():
        available_batches = StockBatch.objects.filter(
            product=item.product,
            location=transfer.source_location,
            quantity__gt=0
        ).order_by('expiry_date')

        needed = item.quantity_requested
        allocated_batches = []

        for batch in available_batches:
            if needed <= 0:
                break
            take_qty = min(batch.quantity, needed)
            allocated_batches.append({'batch_obj': batch, 'take_qty': take_qty})
            needed -= take_qty

        processed_items.append({
            'item': item,
            'allocated': allocated_batches,
            'fulfilled_qty': item.quantity_requested - needed,
            'shortfall': needed > 0
        })

    if request.method == 'POST':
        # Action: Dispatch (Move to Transit)
        for p_item in processed_items:
            total_sent_for_item = 0
            for alloc in p_item['allocated']:
                source_batch = alloc['batch_obj']
                qty_to_move = alloc['take_qty']

                # 1. Deduct from Source
                source_batch.quantity -= qty_to_move
                source_batch.save()

                total_sent_for_item += qty_to_move

            # Update Item Record with actual sent amount
            p_item['item'].quantity_sent = total_sent_for_item
            p_item['item'].save()

        transfer.status = StockTransfer.Status.IN_TRANSIT
        transfer.approved_by = user
        transfer.save()

        messages.success(request, f"Transfer {transfer.reference_number} dispatched. Status: IN TRANSIT.")
        return redirect('inventory:transfer_list')

    return render(request, 'inventory/transfer_detail.html', {
        'transfer': transfer,
        'processed_items': processed_items
    })


@login_required
@role_required(*INVENTORY_STAFF)
@transaction.atomic
def receive_transfer(request, pk):
    """
    SHOP/DESTINATION ACTION: Receipt Confirmation.
    1. User counts physical goods.
    2. Enters 'Quantity Received'.
    3. System ADDS stock to Destination.

    A transfer that is still PENDING is dispatched (deducted from the source)
    on the spot, so one person can collect and confirm in a single step without
    needing someone at the warehouse to press "Process" first.
    """
    transfer = get_object_or_404(StockTransfer, pk=pk)
    user = request.user

    # Security: Ensure user is at either end of the transfer OR is Owner
    if user.role != 'OWNER' and user.assigned_location not in (transfer.destination_location,
                                                               transfer.source_location):
        messages.error(request, "You can only receive transfers involving your location.")
        return redirect('inventory:transfer_list')

    receivable = (StockTransfer.Status.IN_TRANSIT, StockTransfer.Status.PENDING_APPROVAL)
    if transfer.status not in receivable:
        messages.error(request, "This transfer is not waiting to be received.")
        return redirect('inventory:transfer_list')

    if request.method == 'POST':
        needs_dispatch = transfer.status == StockTransfer.Status.PENDING_APPROVAL

        # Process Reception
        for item in transfer.items.all():
            # Get input qty from form
            received_qty_str = request.POST.get(f'received_qty_{item.id}')
            received_qty = int(received_qty_str) if received_qty_str else 0

            if needs_dispatch:
                # No one dispatched this from the source yet — do it now (FEFO).
                item.quantity_sent = _pull_stock_from(item.product, transfer.source_location, received_qty)
                if item.quantity_sent < received_qty:
                    messages.warning(
                        request,
                        f"{transfer.source_location.name} only had {item.quantity_sent} of "
                        f"{received_qty} x {item.product.name} on record."
                    )

            item.quantity_received = received_qty
            item.save()

            # Add to Destination Inventory
            # We create a new batch representing this transfer
            StockBatch.objects.create(
                product=item.product,
                location=transfer.destination_location,
                quantity=received_qty,
                # Ideally cost comes from source batches, using product cost as fallback
                cost_price=item.product.cost_price,
                batch_number=f"TRF-{transfer.reference_number}",
                received_date=timezone.now()
            )

        notes = request.POST.get('notes', '').strip()
        if notes:
            transfer.notes = "\n".join(filter(None, [transfer.notes, notes]))

        transfer.status = StockTransfer.Status.RECEIVED
        transfer.received_by = user
        if needs_dispatch and not transfer.approved_by:
            transfer.approved_by = user
        transfer.save()

        messages.success(request, f"Transfer {transfer.reference_number} received and stock updated.")
        return redirect('inventory:transfer_list')

    return render(request, 'inventory/receive_transfer.html', {'transfer': transfer})


@login_required
@role_required(*INVENTORY_STAFF)
@transaction.atomic
def stock_adjustments(request):
    """
    Record damages, losses, or corrections.
    """
    user = request.user
    # Determine location context
    if user.role == 'OWNER':
        # Owner defaults to first location if not specified, or handles globally?
        # Ideally, adjustments should be specific.
        # For simplicity, we use the GET param or default to the first active location.
        loc_id = request.GET.get('location')
        if loc_id:
            location = Location.objects.get(id=loc_id)
        else:
            location = Location.objects.filter(is_active=True).first()
    else:
        location = user.assigned_location

    if request.method == 'POST':
        form = StockAdjustmentForm(location, request.POST)
        if form.is_valid():
            adjustment = form.save(commit=False)
            adjustment.location = location
            adjustment.performed_by = user
            adjustment.save()

            # Logic: Update the actual batch quantity
            batch = adjustment.batch
            batch.quantity += adjustment.adjusted_quantity
            batch.save()

            messages.warning(request,
                             f"Stock adjusted: {adjustment.adjusted_quantity} units ({adjustment.get_reason_display()})")
            return redirect('inventory:adjustments')
    else:
        form = StockAdjustmentForm(location)

    # History
    history = StockAdjustment.objects.filter(location=location).select_related('batch__product',
                                                                               'performed_by').order_by('-created_at')[
        :20]

    return render(request, 'inventory/adjustments.html', {'form': form, 'history': history})


@login_required
@role_required(*INVENTORY_STAFF)
def expiry_alerts(request):
    """
    View for managing expiring stock.
    """
    user = request.user
    if user.role == 'OWNER':
        # Show all expiring items across all locations
        expiring_batches = StockBatch.objects.all()
    else:
        expiring_batches = StockBatch.objects.filter(location=user.assigned_location)

    today = timezone.now().date()
    thirty_days = today + timedelta(days=30)

    expiring_batches = expiring_batches.filter(
        expiry_date__lte=thirty_days,
        quantity__gt=0
    ).select_related('product', 'location').order_by('expiry_date')

    return render(request, 'inventory/expiry_alerts.html', {
        'expiring_batches': expiring_batches,
        'today': today
    })