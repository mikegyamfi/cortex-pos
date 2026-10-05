"""
The one place money is turned into a Sale.

Both the POS (`process_sale`) and selling to a distributor from their page go
through `resolve_cart` + `record_sale`, so there is a single implementation of
price resolution, FEFO stock deduction, tax split, payments, change and drawer
reconciliation. Anything that records revenue must use these.

Settling arrears (`record_settlement`) and refunds (`record_refund`) live here
too, so every movement of money follows the same rules:

  * every SalePayment row names the drawer (RegisterSession) it went through;
  * drawer, stock and customer counters move with single atomic UPDATEs, never
    read-modify-write, so double clicks and concurrent tills cannot lose or
    double-count anything;
  * amounts and quantities are parsed strictly — a bad keystroke is refused,
    never silently turned into 0 or truncated.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db.models import F
from django.utils import timezone

from apps.customers.models import Customer
from apps.inventory.models import StockAdjustment, StockBatch
from apps.notifications.services import SMSService
from apps.products.models import Product

from .models import RegisterSession, Sale, SaleItem, SaleTax, SalePayment

TWO_PLACES = Decimal('0.01')
ZERO = Decimal('0.00')

# Largest amount a 12-digit, 2dp money column can hold. Anything above it is a
# typo (or an attack), never a real till amount.
MAX_MONEY = Decimal('9999999999.99')
# More units than any shop sells on one line; stops absurd input early.
MAX_LINE_QTY = 100000

# Methods that physically pass through a drawer bucket.
DRAWER_METHODS = ('CASH', 'MOMO', 'CARD')


def _q(value):
    """Quantize a Decimal to 2dp."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


class SaleError(Exception):
    """A sale that must not be recorded. The message is safe to show the user."""


def shift_is_stale(session):
    """
    True when an open shift was started on an earlier day.

    A shift left open overnight piles several days' cash into one drawer, so
    no single day can be counted. Money can't go through it until it is
    closed and today's shift is opened.
    """
    return session is not None and timezone.localtime(session.start_time).date() < timezone.localdate()


def stale_shift_message(session):
    started = timezone.localtime(session.start_time)
    return (f"Your shift from {started:%a %d %b} is still open. Count the cash in the drawer and "
            f"close it, then open today's shift before taking any more money.")


def parse_money(raw, what='Amount'):
    """
    Read a user-supplied amount as a 2dp Decimal, or raise SaleError.

    Blank means zero. Words, NaN/Infinity and amounts too big for the database
    are refused, so a bad keystroke can never become a silent 0 or a crash.
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ''):
        return ZERO
    if isinstance(raw, bool):
        raise SaleError(f'{what} must be a number.')
    try:
        value = Decimal(str(raw).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise SaleError(f'{what} must be a number.')
    if not value.is_finite():
        raise SaleError(f'{what} must be a number.')
    value = _q(value)
    if abs(value) > MAX_MONEY:
        raise SaleError(f'{what} is too large.')
    return value


def parse_qty(raw):
    """
    Read a line quantity as a whole number, or raise SaleError.

    int() used to truncate 2.5 to 2 and turn True into 1; a quantity is now
    an exact whole number or the sale is refused.
    """
    if isinstance(raw, bool) or raw is None:
        raise SaleError('Malformed cart entry.')
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise SaleError('Malformed cart entry.')
    if not value.is_finite() or value != value.to_integral_value():
        raise SaleError('Quantities must be whole numbers.')
    qty = int(value)
    if qty < 0:
        raise SaleError('Quantities cannot be negative. Use a refund to take goods back.')
    if qty > MAX_LINE_QTY:
        raise SaleError('Quantity is too large.')
    return qty


def bump_session(session, cash=ZERO, momo=ZERO, card=ZERO):
    """
    Add to a drawer's running totals with one UPDATE ... SET x = x + n.

    Read-modify-write (`session.total += n; session.save()`) loses money when
    two requests hit the same drawer at once — e.g. a double-clicked button.
    """
    if session is None or not (cash or momo or card):
        return
    RegisterSession.objects.filter(pk=session.pk).update(
        total_cash_sales=F('total_cash_sales') + cash,
        total_momo_sales=F('total_momo_sales') + momo,
        total_card_sales=F('total_card_sales') + card,
    )
    session.refresh_from_db(fields=['total_cash_sales', 'total_momo_sales', 'total_card_sales'])


def _bucket(method, amount):
    """{'cash'|'momo'|'card': amount} for bump_session; empty for bank/cheque."""
    return {method.lower(): amount} if method in DRAWER_METHODS else {}


def _sale_payment_methods():
    """Methods that are real money. CREDIT ("on account") is the absence of it."""
    return {m.value for m in SalePayment.PaymentMethod} - {SalePayment.PaymentMethod.CREDIT}


def resolve_cart(cart, location, default_tier=Product.PriceTier.RETAIL):
    """
    Turn raw cart entries into priced lines, taking every amount from the
    catalogue rather than the caller.

    `cart` entries: {'id': pk, 'qty': n, 'tier': 'RETAIL'|..., 'price': optional claim}

    Returns (resolved, qty_per_product) where resolved is
    [{'pid', 'qty', 'unit_price', 'tier', 'product'}]. Raises SaleError.
    """
    if not isinstance(cart, list):
        raise SaleError('Malformed cart.')

    qty_per_product = {}
    resolved = []

    for entry in cart:
        if not isinstance(entry, dict):
            raise SaleError('Malformed cart entry.')
        try:
            pid = int(entry['id'])
        except (KeyError, TypeError, ValueError):
            raise SaleError('Malformed cart entry.')
        qty = parse_qty(entry.get('qty'))
        if qty == 0:
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

    for pid, total_qty in qty_per_product.items():
        if total_qty > MAX_LINE_QTY:
            raise SaleError('Quantity is too large.')

    return resolved, qty_per_product


def _lock_batches(qty_per_product, location, resolved):
    """Lock and check stock up front so a sale never half-completes."""
    batches_by_product = {}
    for pid, qty_needed in qty_per_product.items():
        batches = list(
            StockBatch.objects.select_for_update()
            .filter(product_id=pid, location=location, quantity__gt=0)
            .order_by('expiry_date', 'received_date', 'id')
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
                session=None, customer=None, order_type=None, notes='', client_ref=None):
    """
    Create the Sale, deduct stock FEFO, split tax, take payments, balance change
    and reconcile the drawer. Must be called inside a transaction — any
    SaleError raised here means the caller must roll back.

    `session` may be None (e.g. selling to a distributor from the office), in
    which case cash cannot be accepted — there is no drawer to put it in.

    Returns the saved Sale.
    """
    if not isinstance(payments, list):
        raise SaleError('Malformed payments.')

    total_amount = _q(sum((r['unit_price'] * r['qty'] for r in resolved), ZERO))
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
        client_ref=client_ref or None,
        **({'order_type': order_type} if order_type else {}),
    )

    sale_subtotal = ZERO
    sale_tax_total = ZERO
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

            # Conditional UPDATE: only succeeds if the units are still there.
            # Where SELECT ... FOR UPDATE is a no-op (SQLite) this is what stops
            # two tills selling the same last units twice.
            took = StockBatch.objects.filter(pk=batch.pk, quantity__gte=take).update(
                quantity=F('quantity') - take
            )
            if took != 1:
                raise SaleError(
                    f'Stock for {product.name} changed while this sale was being saved. '
                    f'Nothing was charged — please try again.'
                )

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
            qty_remaining -= take

        if qty_remaining > 0:
            # Should never happen — availability was validated under lock above.
            raise SaleError(
                f'Stock for {product.name} ran short while saving. Nothing was charged — please try again.'
            )

        # Tax / subtotal split (unit_price is VAT-inclusive)
        line_total = _q(unit_price * Decimal(r['qty']))
        rate = Decimal(product.tax_rate or 0)
        if rate > 0:
            line_tax = _q(line_total * rate / (Decimal('100') + rate))
            line_excl = _q(line_total - line_tax)
            tax_by_rate[rate] = tax_by_rate.get(rate, ZERO) + line_tax
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
    total_paid = ZERO
    total_cash_tendered = ZERO
    drawer = {}
    valid_methods = {m.value for m in SalePayment.PaymentMethod}

    for pay in payments:
        if not isinstance(pay, dict):
            raise SaleError('Malformed payments.')
        amount = parse_money(pay.get('amount', 0), 'Payment amount')
        method = pay.get('method')
        if amount < 0:
            raise SaleError('Payment amounts cannot be negative.')
        if amount == 0:
            continue
        if method not in valid_methods:
            raise SaleError(f'Invalid payment method: {method}.')
        if method == SalePayment.PaymentMethod.CREDIT:
            # "On account" means nothing was received. Counting it as paid
            # would mark a debt as settled with no money in hand.
            raise SaleError(
                'Store credit is not a payment. Leave the balance unpaid with a '
                'customer selected to sell on account.'
            )
        if method == 'CASH' and session is None:
            raise SaleError(
                'Cash cannot be accepted without an open register. '
                'Open a register, or record this as mobile money, card or credit.'
            )

        SalePayment.objects.create(sale=sale, payment_method=method, amount=amount,
                                   processed_by=user, register_session=session)
        total_paid += amount
        if method == 'CASH':
            total_cash_tendered += amount
        if method in DRAWER_METHODS:
            drawer[method] = drawer.get(method, ZERO) + amount

    change_due = max(ZERO, total_paid - total_amount)
    if change_due > total_cash_tendered:
        # Change comes out of the cash drawer. Over-paying by MoMo/card and
        # "giving change" in cash leaves the drawer short at close.
        raise SaleError(
            f'Mobile money / card payments are {change_due - total_cash_tendered} more than the bill. '
            f'Change can only be given from cash — check the amounts.'
        )
    if change_due > 0:
        SalePayment.objects.create(
            sale=sale, payment_method=SalePayment.PaymentMethod.CASH,
            amount=-change_due, reference_id='CHANGE GIVEN', processed_by=user,
            register_session=session,
        )
        total_paid -= change_due
        drawer['CASH'] = drawer.get('CASH', ZERO) - change_due

    if total_paid < total_amount and customer is None:
        raise SaleError(
            f'{total_amount - total_paid} is still unpaid. Select a customer to sell '
            f'on credit, or take the full amount.'
        )

    sale.amount_paid = total_paid
    sale.change_due = change_due
    sale.save()

    bump_session(session, **{m.lower(): a for m, a in drawer.items()})

    # ---------------- customer stats + SMS ----------------
    if customer is not None:
        Customer.objects.filter(pk=customer.pk).update(
            total_spent=F('total_spent') + sale.total_amount,
            total_visits=F('total_visits') + 1,
            last_visit_date=timezone.now(),
        )
        customer.refresh_from_db(fields=['total_spent', 'total_visits', 'last_visit_date'])

        if customer.accepts_marketing_sms and SMSService:
            try:
                SMSService.send_receipt(sale)
            except Exception as sms_error:  # never let SMS break a sale
                print(f'SMS Error: {sms_error}')

    return sale


def record_settlement(*, sale, user, session, tendered):
    """
    Settle arrears on an existing sale. `tendered` is [(method, Decimal)].

    The sale row is locked so a double-submitted form cannot settle twice, and
    a sale with nothing owing is refused rather than "paid" and changed back.
    Returns (total_tendered, change_due). Raises SaleError.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.status in (Sale.Status.REFUNDED, Sale.Status.CANCELLED):
        raise SaleError(f'This sale is {sale.get_status_display().lower()} — nothing can be paid on it.')

    valid = _sale_payment_methods()
    clean = []
    for method, amount in tendered:
        if method not in valid:
            raise SaleError(f'Invalid payment method: {method}.')
        if amount < 0:
            raise SaleError('Payment amounts cannot be negative.')
        if amount > 0:
            clean.append((method, amount))

    total_tendered = sum((a for _, a in clean), ZERO)
    if total_tendered <= 0:
        raise SaleError('Enter a valid payment amount.')

    balance = max(ZERO, sale.total_amount - sale.amount_paid)
    if balance <= 0:
        raise SaleError('Nothing is owed on this sale.')

    cash_tendered = sum((a for m, a in clean if m == 'CASH'), ZERO)
    change_due = max(ZERO, total_tendered - balance)
    if change_due > cash_tendered:
        raise SaleError(
            f'Mobile money / card payments are {change_due - cash_tendered} more than the balance of '
            f'{balance}. Change can only be given from cash — check the amounts.'
        )

    for method, amount in clean:
        SalePayment.objects.create(
            sale=sale, payment_method=method, amount=amount, processed_by=user,
            is_settlement=True, reference_id='DEBT SETTLEMENT', register_session=session,
        )
        bump_session(session, **_bucket(method, amount))

    if change_due > 0:
        SalePayment.objects.create(
            sale=sale, payment_method=SalePayment.PaymentMethod.CASH,
            amount=-change_due, reference_id='CHANGE GIVEN', processed_by=user,
            register_session=session,
        )
        bump_session(session, cash=-change_due)

    sale.amount_paid = sale.amount_paid + total_tendered - change_due
    sale.change_due += change_due
    # Only a draft (PENDING) sale graduates to COMPLETED here; credit sales are
    # already COMPLETED and partially-refunded sales keep their status.
    if sale.status == Sale.Status.PENDING_PAYMENT and sale.amount_paid >= sale.total_amount:
        sale.status = Sale.Status.COMPLETED
    sale.save(update_fields=['amount_paid', 'change_due', 'status', 'updated_at'])
    return total_tendered, change_due


def record_refund(*, sale, user, session, item_ids, method, reason):
    """
    Refund whole sale lines: restock, reduce the bill, and pay back only what
    the customer actually paid for.

    On a credit sale the returned goods first cancel what is still owed; only
    money actually received beyond the new bill goes back out of the drawer.
    (Paying out the full value of goods that were never paid for empties the
    till of money it never received.)

    Returns (refund_value, money_returned). Raises SaleError.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if method not in _sale_payment_methods():
        raise SaleError(f'Invalid refund method: {method}.')

    try:
        ids = {int(i) for i in item_ids}
    except (TypeError, ValueError):
        raise SaleError('Invalid item selection.')

    items = list(SaleItem.objects.select_for_update().filter(sale=sale, id__in=ids, is_refunded=False))
    if not items:
        raise SaleError('No items selected for refund.')

    refund_value = ZERO
    for item in items:
        # Flip the flag with a guarded UPDATE so the same line can never be
        # restocked twice, even if two refund forms are submitted together.
        if SaleItem.objects.filter(pk=item.pk, is_refunded=False).update(is_refunded=True) != 1:
            continue
        if item.source_batch_id:
            StockBatch.objects.filter(pk=item.source_batch_id).update(
                quantity=F('quantity') + item.quantity
            )
            StockAdjustment.objects.create(
                location=sale.location,
                batch_id=item.source_batch_id,
                adjusted_quantity=item.quantity,
                reason='RETURN',
                notes=f'Refund for Invoice #{sale.invoice_number} ({reason})',
                performed_by=user,
            )
        refund_value += item.total_price

    refund_value = _q(refund_value)
    if refund_value <= 0:
        raise SaleError('No items selected for refund.')

    new_total = max(ZERO, _q(sale.total_amount - refund_value))
    money_returned = max(ZERO, _q(sale.amount_paid - new_total))

    if money_returned > 0:
        if method == 'CASH' and session is None:
            raise SaleError('You must have an open register to return cash.')
        SalePayment.objects.create(
            sale=sale,
            payment_method=method,
            amount=-money_returned,
            reference_id=f'REFUND - {reason}'[:100],
            processed_by=user,
            register_session=session,
        )
        bump_session(session, **_bucket(method, -money_returned))

    sale.total_amount = new_total
    sale.amount_paid = _q(sale.amount_paid - money_returned)
    all_refunded = not sale.items.filter(is_refunded=False).exists()
    sale.status = Sale.Status.REFUNDED if all_refunded else Sale.Status.PARTIAL_REFUND
    sale.save(update_fields=['total_amount', 'amount_paid', 'status', 'updated_at'])

    # Customer stats — returned goods were not spent. Visits stay (the visit happened).
    if sale.customer_id:
        Customer.objects.filter(pk=sale.customer_id).update(total_spent=F('total_spent') - refund_value)
        Customer.objects.filter(pk=sale.customer_id, total_spent__lt=0).update(total_spent=ZERO)

    return refund_value, money_returned


def receipt_lines_for_sale(sale):
    """
    Receipt lines rebuilt from what was stored — used when a retried checkout
    returns an already-recorded sale. Batch splits are merged back into one
    line per product and price list.
    """
    lines = {}
    for item in sale.items.select_related('product').order_by('id'):
        key = (item.product_id, item.price_tier, item.unit_price)
        line = lines.get(key)
        if line is None:
            line = lines[key] = {
                'product_id': item.product_id,
                'name': item.product.name,
                'sku': item.product.sku,
                'qty': 0,
                'tier': item.price_tier,
                'tier_label': Product.PriceTier(item.price_tier).label,
                'unit_price': float(item.unit_price),
                'line_total': ZERO,
            }
        line['qty'] += item.quantity
        line['line_total'] += item.total_price
    out = list(lines.values())
    for line in out:
        line['line_total'] = float(line['line_total'])
    return out


def receipt_lines(resolved):
    """The lines as recorded, for printing a receipt from server figures."""
    return [{
        'product_id': r['pid'],
        'name': r['product'].name,
        'sku': r['product'].sku,
        'qty': r['qty'],
        'tier': r['tier'],
        'tier_label': Product.PriceTier(r['tier']).label,
        'unit_price': float(r['unit_price']),
        'line_total': float(_q(r['unit_price'] * r['qty'])),
    } for r in resolved]
