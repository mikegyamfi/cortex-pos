"""
Distributors.

A distributor is a Customer with is_distributor=True, so everything that already
works for customers — debt, arrears, statements, SMS — works for them unchanged.
They get their own pages, and products can be sold to them straight from their
detail page at the distributor price list.

Selling goes through apps.sales.services, the same money engine as the POS, so
prices, stock deduction, tax, payments and the drawer behave identically.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.decorators import MANAGEMENT, role_required
from apps.products.models import Product
from apps.sales.models import RegisterSession
from apps.sales.services import SaleError, record_sale, resolve_cart
from apps.sales.views import _location_stock_map

from .forms import DistributorForm
from .models import Customer


def _distributor_scope(user):
    """Distributors of the user's shop; owners see every shop's."""
    qs = Customer.objects.filter(is_distributor=True)
    if user.role != 'OWNER':
        qs = qs.filter(location=user.assigned_location)
    return qs


@login_required
@role_required(*MANAGEMENT)
def distributor_list(request):
    query = (request.GET.get('q') or '').strip()
    distributors = _distributor_scope(request.user).select_related('location')

    if query:
        distributors = distributors.filter(
            Q(company_name__icontains=query)
            | Q(contact_person__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone_number__icontains=query)
            | Q(backup_phone__icontains=query)
            | Q(email__icontains=query)
            | Q(address__icontains=query)
        )

    distributors = distributors.annotate(
        purchase_count=Count('purchases', distinct=True),
        billed=Sum('purchases__total_amount'),
        collected=Sum('purchases__amount_paid'),
    ).order_by('company_name', 'first_name')

    rows = []
    for d in distributors:
        billed = d.billed or Decimal('0.00')
        collected = d.collected or Decimal('0.00')
        rows.append({
            'distributor': d,
            'purchase_count': d.purchase_count,
            'billed': billed,
            'owing': max(billed - collected, Decimal('0.00')),
        })

    return render(request, 'customers/distributor_list.html', {
        'rows': rows,
        'query': query,
        'total_billed': sum((r['billed'] for r in rows), Decimal('0.00')),
        'total_owing': sum((r['owing'] for r in rows), Decimal('0.00')),
    })


@login_required
@role_required(*MANAGEMENT)
def distributor_create(request):
    if request.method == 'POST':
        form = DistributorForm(request.POST)
        # Bind to the creator's shop before validation so the per-shop phone
        # uniqueness check runs against the right shop.
        form.instance.location = request.user.assigned_location
        if form.is_valid():
            distributor = form.save()
            messages.success(request, f"Distributor {distributor.get_display_name} added.")
            return redirect('customers:distributor_detail', pk=distributor.pk)
    else:
        form = DistributorForm()
    return render(request, 'customers/distributor_form.html',
                  {'form': form, 'title': 'Add Distributor'})


@login_required
@role_required(*MANAGEMENT)
def distributor_edit(request, pk):
    distributor = get_object_or_404(_distributor_scope(request.user), pk=pk)
    if request.method == 'POST':
        form = DistributorForm(request.POST, instance=distributor)
        if form.is_valid():
            form.save()
            messages.success(request, f"{distributor.get_display_name} updated.")
            return redirect('customers:distributor_detail', pk=distributor.pk)
    else:
        form = DistributorForm(instance=distributor)
    return render(request, 'customers/distributor_form.html', {
        'form': form,
        'title': f"Edit {distributor.get_display_name}",
        'distributor': distributor,
    })


@login_required
@role_required(*MANAGEMENT)
def distributor_detail(request, pk):
    """Profile, purchase history, and the form that sells to them."""
    distributor = get_object_or_404(_distributor_scope(request.user), pk=pk)
    location = distributor.location or request.user.assigned_location

    purchases = distributor.purchases.select_related('location').order_by('-created_at')[:50]
    stats = distributor.purchases.aggregate(
        billed=Sum('total_amount'), collected=Sum('amount_paid'), count=Count('id'),
    )
    billed = stats['billed'] or Decimal('0.00')
    collected = stats['collected'] or Decimal('0.00')

    # Only products this shop prices for distributors can be sold here — the
    # rest would be refused by the pricing rules anyway.
    sellable = list(Product.objects.filter(
        is_active=True, location=location, distributor_price__isnull=False
    ).order_by('name'))
    stock = _location_stock_map(location, [p.id for p in sellable])
    for product in sellable:
        product.on_hand = stock.get(product.id, 0)

    unpriced_count = Product.objects.filter(
        is_active=True, location=location, distributor_price__isnull=True
    ).count()

    open_session = RegisterSession.objects.filter(
        user=request.user, location=location, status=RegisterSession.Status.OPEN
    ).first()

    return render(request, 'customers/distributor_detail.html', {
        'distributor': distributor,
        'purchases': purchases,
        'billed': billed,
        'collected': collected,
        'owing': max(billed - collected, Decimal('0.00')),
        'purchase_count': stats['count'] or 0,
        'sellable': sellable,
        'unpriced_count': unpriced_count,
        'location': location,
        'open_session': open_session,
    })


@login_required
@role_required(*MANAGEMENT)
@require_POST
@transaction.atomic
def distributor_sell(request, pk):
    """
    Sell to a distributor at the distributor price list.

    The posted form carries quantities only. Every amount is looked up in the
    catalogue by the shared money engine, stock comes off FEFO, and anything
    unpaid lands on the distributor's account as debt.
    """
    distributor = get_object_or_404(_distributor_scope(request.user), pk=pk)
    location = distributor.location or request.user.assigned_location

    cart = []
    for key, raw in request.POST.items():
        if not key.startswith('qty_'):
            continue
        try:
            qty = int(raw or 0)
        except (TypeError, ValueError):
            messages.error(request, "Quantities must be whole numbers.")
            return redirect('customers:distributor_detail', pk=pk)
        if qty > 0:
            cart.append({'id': key[4:], 'qty': qty, 'tier': Product.PriceTier.DISTRIBUTOR})

    if not cart:
        messages.warning(request, "Enter a quantity for at least one product.")
        return redirect('customers:distributor_detail', pk=pk)

    method = request.POST.get('payment_method') or 'CREDIT'
    payments = []
    if method != 'CREDIT':
        try:
            amount = Decimal((request.POST.get('amount_paid') or '0').strip() or '0')
        except (ArithmeticError, ValueError):
            messages.error(request, "Amount paid must be a number.")
            return redirect('customers:distributor_detail', pk=pk)
        if amount > 0:
            payments.append({'method': method, 'amount': amount})

    session = RegisterSession.objects.filter(
        user=request.user, location=location, status=RegisterSession.Status.OPEN
    ).first()

    try:
        resolved, qty_per_product = resolve_cart(
            cart, location, default_tier=Product.PriceTier.DISTRIBUTOR,
        )
        sale = record_sale(
            location=location, user=request.user, session=session, customer=distributor,
            resolved=resolved, qty_per_product=qty_per_product, payments=payments,
            notes=f"Distributor sale to {distributor.get_display_name}",
        )
    except SaleError as err:
        transaction.set_rollback(True)
        messages.error(request, str(err))
        return redirect('customers:distributor_detail', pk=pk)

    if sale.balance_remaining > 0:
        messages.warning(request, (
            f"Sale {sale.invoice_number} recorded: {sale.total_amount} billed, "
            f"{sale.amount_paid} paid, {sale.balance_remaining} left on account."
        ))
    else:
        messages.success(request, (
            f"Sale {sale.invoice_number} recorded: {sale.total_amount} paid in full."
        ))
    return redirect('sales:detail', pk=sale.pk)
