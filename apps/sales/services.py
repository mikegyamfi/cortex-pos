"""
The one place money is turned into a Sale.

Both the POS (`process_sale`) and selling to a distributor from their page go
through `resolve_cart` + `record_sale`, so there is a single implementation of
price resolution, FEFO stock deduction, tax split, payments, change and drawer
reconciliation. Anything that records revenue must use these.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.customers.models import Customer
from apps.inventory.models import StockBatch
from apps.notifications.services import SMSService
from apps.products.models import Product

from .models import Sale, SaleItem, SaleTax, SalePayment

TWO_PLACES = Decimal('0.01')


def _q(value):
    """Quantize a Decimal to 2dp."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


class SaleError(Exception):
    """A sale that must not be recorded. The message is safe to show the user."""


def resolve_cart(cart, location, default_tier=Product.PriceTier.RETAIL):
    """
    Turn raw cart entries into priced lines, taking every amount from the
    catalogue rather than the caller.

    `cart` entries: {'id': pk, 'qty': n, 'tier': 'RETAIL'|..., 'price': optional claim}

    Returns (resolved, qty_per_product) where resolved is
    [{'pid', 'qty', 'unit_price', 'tier', 'product'}]. Raises SaleError.
    """
    qty_per_product = {}
    resolved = []

    for entry in cart:
        try:
            pid = int(entry['id'])
            qty = int(entry['qty'])
        except (KeyError, TypeError, ValueError):
            raise SaleError('Malformed cart entry.')
        if qty <= 0:
            continue

        try:
            product = Product.objects.get(id=pid, is_active=True, location=location)
        except Product.DoesNotExist:
            raise SaleError(f'Product {pid} not found.')

        tier = str(entry.get('tier') or default_tier).upper()
        if tier not in Product.PriceTier.values:
            raise SaleError(f'Unknown price type {tier!r} for {product.name}.')

        tier_price = product.price_for_tier(tier)
        if tier_price is None:
            label = Product.PriceTier(tier).label
            raise SaleError(
                f'{product.name} has no {label.lower()} price set. '
                f'Set one on the product before selling at that price.'
            )

        unit_price = _q(tier_price)
        if unit_price <= 0:
            raise SaleError(f'{product.name} has an invalid price of {unit_price}.')

        # Any price the caller claimed is a claim to verify, never a source.
        claimed = entry.get('price', None)
        if claimed not in (None, ''):
            try:
                if _q(str(claimed)) != unit_price:
                    raise ValueError
            except (ValueError, ArithmeticError):
                raise SaleError(
                    f'Price for {product.name} is out of date. Please refresh and try again.'
                )

        qty_per_product[pid] = qty_per_product.get(pid, 0) + qty
        resolved.append({'pid': pid, 'qty': qty, 'unit_price': unit_price,
                         'tier': tier, 'product': product})

    if not resolved:
        raise SaleError('Cart is empty.')

    return resolved, qty_per_product


def _lock_batches(qty_per_product, location, resolved):
    """Lock and check stock up front so a sale never half-completes."""
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
            raise SaleError(
                f'Insufficient stock for {name}. Requested {qty_needed}, available {available}.'
            )
        batches_by_product[pid] = batches
    return batches_by_product


def record_sale(*, location, user, resolved, qty_per_product, payments,
                session=None, customer=None, order_type=None, notes=''):
    """
    Create the Sale, deduct stock FEFO, split tax, take payments, balance change
    and reconcile the drawer. Must be called inside a transaction.

    `session` may be None (e.g. selling to a distributor from the office), in
    which case cash cannot be accepted — there is no drawer to put it in.

    Returns the saved Sale.
    """
    total_amount = _q(sum((r['unit_price'] * r['qty'] for r in resolved), Decimal('0.00')))
    batches_by_product = _lock_batches(qty_per_product, location, resolved)

    sale = Sale.objects.create(
        location=location,
        cashier=user,
        register_session=session,
        total_amount=total_amount,
        status=Sale.Status.COMPLETED,
        amount_paid=0,
        customer=customer,
        notes=notes,
        **({'order_type': order_type} if order_type else {}),
    )

    sale_subtotal = Decimal('0.00')
    sale_tax_total = Decimal('0.00')
    tax_by_rate = {}

    for r in resolved:
        pid = r['pid']
        unit_price = r['unit_price']
        qty_remaining = r['qty']
        product = r['product']

        for batch in batches_by_product[pid]:
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
                price_tier=r['tier'],
            )

            batch.quantity -= take
            batch.save()
            qty_remaining -= take

        if qty_remaining > 0:
            # Should never happen — availability was validated under lock above.
            raise RuntimeError(
                f'Stock validation passed but ran short during allocation for {product.name}.'
            )

        # Tax / subtotal split (unit_price is VAT-inclusive)
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

    for rate, amt in tax_by_rate.items():
        if amt > 0:
            SaleTax.objects.create(sale=sale, tax_name='VAT', tax_rate=rate, tax_amount=_q(amt))

    # ---------------- payments, change, drawer ----------------
    total_paid = Decimal('0.00')
    total_cash_tendered = Decimal('0.00')
    valid_methods = {m.value for m in SalePayment.PaymentMethod}

    for pay in payments:
        try:
            amount = _q(str(pay.get('amount', 0)))
        except (ValueError, ArithmeticError):
            raise SaleError('Invalid payment amount.')
        method = pay.get('method')
        if amount <= 0:
            continue
        if method not in valid_methods:
            raise SaleError(f'Invalid payment method: {method}.')
        if method == 'CASH' and session is None:
            raise SaleError(
                'Cash cannot be accepted without an open register. '
                'Open a register, or record this as mobile money, card or credit.'
            )

        SalePayment.objects.create(sale=sale, payment_method=method, amount=amount,
                                   processed_by=user)
        total_paid += amount

        if method == 'CASH':
            total_cash_tendered += amount
        elif method == 'MOMO' and session is not None:
            session.total_momo_sales += amount
        elif method == 'CARD' and session is not None:
            session.total_card_sales += amount

    change_due = max(Decimal('0.00'), total_paid - total_amount)
    if change_due > 0:
        SalePayment.objects.create(
            sale=sale, payment_method=SalePayment.PaymentMethod.CASH,
            amount=-change_due, reference_id='CHANGE GIVEN', processed_by=user,
        )
        total_paid -= change_due

    sale.amount_paid = total_paid
    sale.change_due = change_due
    sale.save()

    if session is not None:
        session.total_cash_sales += (total_cash_tendered - change_due)
        session.save()

    # ---------------- customer stats + SMS ----------------
    if customer is not None:
        customer.total_spent += sale.total_amount
        customer.total_visits += 1
        customer.last_visit_date = timezone.now()
        customer.save()

        if customer.accepts_marketing_sms and SMSService:
            try:
                SMSService.send_receipt(sale)
            except Exception as sms_error:  # never let SMS break a sale
                print(f'SMS Error: {sms_error}')

    return sale


def receipt_lines(resolved):
    """The lines as recorded, for printing a receipt from server figures."""
    return [{
        'name': r['product'].name,
        'sku': r['product'].sku,
        'qty': r['qty'],
        'tier': r['tier'],
        'tier_label': Product.PriceTier(r['tier']).label,
        'unit_price': float(r['unit_price']),
        'line_total': float(_q(r['unit_price'] * r['qty'])),
    } for r in resolved]
