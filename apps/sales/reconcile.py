"""
Find — and where it is safe, repair — sales records that do not tally.

Each check compares a stored running figure with the rows it should equal:

  * a line's total            vs  unit price x quantity
  * a sale's total            vs  its un-refunded lines
  * a sale's amount paid      vs  its payment rows (the ledger)
  * change handed back but never written as a payment row (older versions)
  * a sale's status           vs  its lines and balance
  * a payment's drawer        vs  the shift it happened in   (backfill)
  * a drawer's running totals vs  the payment rows tagged to it
  * a customer's total spent  vs  their sales
  * double-submitted sales    (same till, same goods, seconds apart)
  * stock below zero

Running figures are repaired from the rows, never the other way round: the
rows are what was actually recorded, one by one, at the time.

Things that need a person (a suspected duplicate sale, money refunded that
was never received, negative stock) are only REPORTED — the right fix there
depends on what physically happened in the shop.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from apps.customers.models import Customer
from apps.finance.models import Expense
from apps.inventory.models import StockBatch

from .models import RegisterSession, Sale, SaleItem, SalePayment

ZERO = Decimal('0.00')

# Payments recorded within this long of the sale are part of the checkout itself.
CHECKOUT_WINDOW = timedelta(minutes=2)
# Two identical sales on one till this close together are probably one sale
# submitted twice (double click, retried request).
DUPLICATE_WINDOW = timedelta(minutes=3)


@dataclass
class Finding:
    check: str
    subject: str
    detail: str
    fixable: bool
    fixed: bool = False


@dataclass
class Report:
    findings: list = field(default_factory=list)
    # Drawer movements a repair adds (or, in a dry run, WOULD add):
    # [(session_id, method, amount)]. The drawer check counts them so a dry
    # run reports exactly what a real run would.
    drawer_moves: list = field(default_factory=list)
    # Payment ids already counted through drawer_moves.
    moved_payment_ids: set = field(default_factory=set)

    def add(self, check, subject, detail, fixable):
        f = Finding(check, subject, detail, fixable)
        self.findings.append(f)
        return f

    @property
    def fixable(self):
        return [f for f in self.findings if f.fixable]

    @property
    def review(self):
        return [f for f in self.findings if not f.fixable]


def _money(v):
    return Decimal(v or 0).quantize(Decimal('0.01'))


# ---------------------------------------------------------------- line items

def check_line_totals(report, sales, fix):
    items = SaleItem.objects.filter(sale__in=sales).select_related('sale')
    for item in items.iterator():
        expected = _money(item.unit_price * item.quantity - item.discount_amount)
        if item.total_price != expected:
            f = report.add(
                'line-total', item.sale.invoice_number,
                f'line {item.id}: total {item.total_price} but {item.quantity} x {item.unit_price}'
                f'{" - " + str(item.discount_amount) if item.discount_amount else ""} = {expected}',
                fixable=True,
            )
            if fix:
                SaleItem.objects.filter(pk=item.pk).update(total_price=expected)
                f.fixed = True


# ---------------------------------------------------------------- sales

def check_sales(report, sales, fix):
    line_totals = dict(
        SaleItem.objects.filter(sale__in=sales, is_refunded=False)
        .values_list('sale_id').annotate(t=Sum('total_price'))
    )
    any_refunded = set(
        SaleItem.objects.filter(sale__in=sales, is_refunded=True).values_list('sale_id', flat=True)
    )
    any_kept = set(
        SaleItem.objects.filter(sale__in=sales, is_refunded=False).values_list('sale_id', flat=True)
    )
    ledger = dict(
        SalePayment.objects.filter(sale__in=sales).values_list('sale_id').annotate(t=Sum('amount'))
    )

    for sale in sales.iterator():
        inv = sale.invoice_number
        updates = {}

        # 1. Bill vs its lines.
        lines = _money(line_totals.get(sale.id))
        if sale.status != Sale.Status.CANCELLED and sale.total_amount != lines:
            f = report.add('sale-total', inv,
                           f'bill says {sale.total_amount}, its lines add up to {lines}', fixable=True)
            updates['total_amount'] = lines
            f.fixed = fix
        total = updates.get('total_amount', sale.total_amount)

        # 2. Amount paid vs the payment rows.
        paid_rows = _money(ledger.get(sale.id))
        change = _money(sale.change_due)
        if (paid_rows > total and change > 0 and paid_rows - change == total
                and sale.amount_paid == paid_rows):
            # Older checkouts stored the cash tendered as "paid" plus a
            # change_due, but never wrote the change as a payment row, so the
            # ledger shows the customer paying more than the bill. The change
            # was handed out of the sale's drawer at the time of the sale.
            f = report.add(
                'missing-change', inv,
                f'{change} change was given but never recorded as a payment row '
                f'(rows add up to {paid_rows} on a bill of {total})',
                fixable=True,
            )
            if sale.register_session_id:
                report.drawer_moves.append((sale.register_session_id, 'CASH', -change))
            if fix:
                row = SalePayment.objects.create(
                    sale=sale, payment_method=SalePayment.PaymentMethod.CASH, amount=-change,
                    reference_id='CHANGE GIVEN', processed_by_id=sale.cashier_id,
                    register_session_id=sale.register_session_id,
                )
                # Keep the row at the time of the sale, not today.
                SalePayment.objects.filter(pk=row.pk).update(created_at=sale.created_at)
                # Already counted through drawer_moves; don't count it twice.
                report.moved_payment_ids.add(row.pk)
                f.fixed = True
            updates['amount_paid'] = total
        elif paid_rows < 0:
            report.add(
                'over-refunded', inv,
                f'{-paid_rows} more was paid back than was ever received '
                f'(payments net to {paid_rows}). Cash left the drawer for goods never paid for. '
                f'check with the person who did the refund.',
                fixable=False,
            )
        elif paid_rows > total:
            report.add(
                'overpaid', inv,
                f'payments net to {paid_rows} on a bill of {total} and no change was recorded. '
                f'Either change was handed back without being recorded, or the customer is owed {paid_rows - total}.',
                fixable=False,
            )
        elif sale.amount_paid != paid_rows:
            f = report.add('amount-paid', inv,
                           f'amount paid says {sale.amount_paid}, payment rows add up to {paid_rows}',
                           fixable=True)
            updates['amount_paid'] = paid_rows
            f.fixed = fix
        paid = updates.get('amount_paid', sale.amount_paid)

        # 3. Status vs lines and balance.
        status = sale.status
        if sale.id in any_refunded and sale.id not in any_kept and status != Sale.Status.REFUNDED:
            status = Sale.Status.REFUNDED
        elif sale.id in any_refunded and sale.id in any_kept and status == Sale.Status.COMPLETED:
            status = Sale.Status.PARTIAL_REFUND
        elif status == Sale.Status.PENDING_PAYMENT and total > 0 and paid >= total:
            status = Sale.Status.COMPLETED
        if status != sale.status:
            f = report.add('status', inv, f'status {sale.status} should be {status}', fixable=True)
            updates['status'] = status
            f.fixed = fix

        if fix and updates:
            Sale.objects.filter(pk=sale.pk).update(**updates)


# ---------------------------------------------------------------- duplicates

def check_duplicates(report, sales):
    """Same cashier, same till, same goods and bill, minutes apart."""
    rows = defaultdict(list)
    for item in SaleItem.objects.filter(sale__in=sales).values('sale_id', 'product_id', 'quantity'):
        rows[item['sale_id']].append((item['product_id'], item['quantity']))

    def signature(sale):
        per_product = defaultdict(int)
        for pid, qty in rows.get(sale.id, []):
            per_product[pid] += qty
        return tuple(sorted(per_product.items()))

    recent = {}
    for sale in sales.exclude(status=Sale.Status.CANCELLED).order_by('created_at', 'id').iterator():
        key = (sale.cashier_id, sale.register_session_id, sale.customer_id, sale.total_amount, signature(sale))
        prev = recent.get(key)
        if prev is not None and sale.created_at - prev.created_at <= DUPLICATE_WINDOW:
            secs = int((sale.created_at - prev.created_at).total_seconds())
            units = sum(q for _, q in key[4])
            report.add(
                'possible-duplicate', sale.invoice_number,
                f'same goods ({units} units, {sale.total_amount}) as {prev.invoice_number} by the same '
                f'cashier {secs}s earlier. If the customer only bought once, refund {sale.invoice_number} '
                f'to put the stock and money back.',
                fixable=False,
            )
        recent[key] = sale


# ---------------------------------------------------------------- drawers

def _session_at(user_id, location_id, when):
    """The one drawer `user` had open at `location` at `when`, or None."""
    qs = RegisterSession.objects.filter(user_id=user_id, start_time__lte=when).filter(
        Q(end_time__gte=when) | Q(end_time__isnull=True)
    )
    if location_id is not None:
        qs = qs.filter(location_id=location_id)
    found = list(qs[:2])
    return found[0] if len(found) == 1 else None


def backfill_payment_drawers(report, sales, fix):
    """
    Older payments do not say which drawer they went through. Work it out:
    payments taken at checkout went into the sale's drawer; later ones
    (settlements, refunds) into the drawer their processor had open then.
    """
    untagged = (
        SalePayment.objects.filter(sale__in=sales, register_session__isnull=True)
        .select_related('sale', 'processed_by')
        .order_by('created_at')
    )
    placed = 0
    unresolved = []
    for pay in untagged.iterator():
        sale = pay.sale
        session = None
        at_checkout = (
            not pay.is_settlement
            and not (pay.reference_id or '').startswith('REFUND')
            and abs(pay.created_at - sale.created_at) <= CHECKOUT_WINDOW
        )
        if at_checkout and sale.register_session_id:
            session = sale.register_session
        elif pay.processed_by_id:
            processor = pay.processed_by
            session = (_session_at(pay.processed_by_id, getattr(processor, 'assigned_location_id', None), pay.created_at)
                       or _session_at(pay.processed_by_id, None, pay.created_at))
        if session is None:
            if pay.payment_method in ('CASH', 'MOMO', 'CARD'):
                unresolved.append(pay)
            continue
        placed += 1
        report.drawer_moves.append((session.id, pay.payment_method, pay.amount))
        report.moved_payment_ids.add(pay.id)
        if fix:
            SalePayment.objects.filter(pk=pay.pk).update(register_session=session)

    if placed:
        f = report.add('payment-drawer', f'{placed} payments',
                       'older payment rows without a drawer can be linked to the shift they happened in',
                       fixable=True)
        f.fixed = fix
    for pay in unresolved:
        report.add('payment-drawer', pay.sale.invoice_number,
                   f'{pay.payment_method} {pay.amount} on {timezone.localtime(pay.created_at):%Y-%m-%d %H:%M} '
                   f'by {pay.processed_by or "unknown"}: no single open drawer at that time, so it cannot be placed',
                   fixable=False)
    return unresolved


def _till_expenses(session):
    qs = Expense.objects.filter(
        location=session.location, is_paid_from_till=True,
        status=Expense.Status.APPROVED, created_at__gte=session.start_time,
    )
    if session.end_time:
        qs = qs.filter(created_at__lte=session.end_time)
    return _money(qs.aggregate(s=Sum('amount'))['s'])


def check_drawers(report, sessions, fix, include_closed, unplaceable_sessions):
    """
    Compare each drawer's running totals with the payment rows that went
    through it — including rows the repairs above add or re-link, so a dry
    run shows the same drawer results a real run would produce.
    """
    extra = defaultdict(lambda: defaultdict(lambda: ZERO))
    for session_id, method, amount in report.drawer_moves:
        extra[session_id][method] += amount
    extra_ids = report.moved_payment_ids

    for session in sessions.select_related('user', 'location').iterator():
        if session.id in unplaceable_sessions:
            continue
        tagged = SalePayment.objects.filter(register_session=session).exclude(pk__in=extra_ids)
        sums = dict(tagged.values_list('payment_method').annotate(t=Sum('amount')))
        ledger = {m: _money(_money(sums.get(m)) + extra[session.id][m]) for m in ('CASH', 'MOMO', 'CARD')}
        stored = {'CASH': session.total_cash_sales, 'MOMO': session.total_momo_sales,
                  'CARD': session.total_card_sales}
        if all(_money(stored[m]) == ledger[m] for m in ledger):
            continue
        if not tagged.exists() and session.id not in extra:
            # No payment rows lead to this drawer — no evidence to repair from.
            continue

        label = (f'shift #{session.id} {session.user} @ {session.location.name} '
                 f'{timezone.localtime(session.start_time):%Y-%m-%d}')
        diffs = ', '.join(f'{m.lower()} {stored[m]} -> {ledger[m]}'
                          for m in ledger if _money(stored[m]) != ledger[m])
        is_open = session.status == RegisterSession.Status.OPEN
        updates = {'total_cash_sales': ledger['CASH'], 'total_momo_sales': ledger['MOMO'],
                   'total_card_sales': ledger['CARD']}

        if is_open:
            f = report.add('drawer', label, f'running totals do not match its payments: {diffs}', fixable=True)
        else:
            expected = _money(session.opening_balance + ledger['CASH'] - _till_expenses(session))
            counted = session.closing_balance_actual
            variance = '' if counted is None else f'; counted {counted}, so variance {counted - expected}'
            f = report.add(
                'drawer', label,
                f'CLOSED shift totals do not match its payments: {diffs}. Expected cash should have been '
                f'{expected} (was {session.closing_balance_expected}){variance}'
                + ('' if include_closed else '. Re-run with --include-closed to correct it.'),
                fixable=include_closed,
            )
            updates['closing_balance_expected'] = expected
            if counted is not None:
                updates['status'] = (RegisterSession.Status.CLOSED if counted == expected
                                     else RegisterSession.Status.DISCREPANCY)
        if fix and f.fixable:
            RegisterSession.objects.filter(pk=session.pk).update(**updates)
            f.fixed = True


# ---------------------------------------------------------------- customers

def check_customers(report, fix, location=None):
    spent = dict(
        Sale.objects.filter(customer__isnull=False).exclude(status=Sale.Status.CANCELLED)
        .values_list('customer_id').annotate(t=Sum('total_amount'))
    )
    customers = Customer.objects.all()
    if location is not None:
        customers = customers.filter(location=location)
    for customer in customers.iterator():
        expected = _money(spent.get(customer.id))
        if _money(customer.total_spent) != expected:
            f = report.add('customer-spend', str(customer),
                           f'total spent says {customer.total_spent}, their sales add up to {expected}',
                           fixable=True)
            if fix:
                Customer.objects.filter(pk=customer.pk).update(total_spent=expected)
                f.fixed = True


# ---------------------------------------------------------------- stock

def check_stock(report, location=None):
    batches = StockBatch.objects.filter(quantity__lt=0).select_related('product', 'location')
    if location is not None:
        batches = batches.filter(location=location)
    for b in batches:
        report.add('negative-stock', f'{b.product.name} @ {b.location.name}',
                   f'batch {b.id} is at {b.quantity}. Count the shelf and correct it with a stock adjustment.',
                   fixable=False)


# ---------------------------------------------------------------- entry point

def reconcile(*, fix=False, include_closed=False, location=None, since=None, until=None,
              include_customers=True):
    """
    Run every check. With fix=True, repair what can be repaired from the rows.
    The caller wraps this in a transaction.

    since/until (dates, inclusive) narrow it to sales made, and shifts open, in
    that window — e.g. one day for the Daily Report.
    """
    report = Report()
    sales = Sale.objects.all()
    sessions = RegisterSession.objects.all()
    if location is not None:
        sales = sales.filter(location=location)
        sessions = sessions.filter(location=location)
    if since is not None:
        sales = sales.filter(created_at__date__gte=since)
        sessions = sessions.filter(Q(end_time__isnull=True) | Q(end_time__date__gte=since))
    if until is not None:
        sales = sales.filter(created_at__date__lte=until)
        sessions = sessions.filter(start_time__date__lte=until)

    check_line_totals(report, sales, fix)
    check_sales(report, sales, fix)
    check_duplicates(report, sales)

    # Payments on these sales, plus settlements/refunds against older sales
    # that went through these drawers.
    pay_sales = Sale.objects.filter(Q(pk__in=sales) | Q(payments__register_session__in=sessions)).distinct()
    unplaced = backfill_payment_drawers(report, pay_sales, fix)
    # A payment we cannot place belongs to one of the drawers its processor
    # had open at the time; don't "repair" those from an incomplete ledger.
    unplaceable_sessions = set()
    for pay in unplaced:
        unplaceable_sessions |= set(
            RegisterSession.objects.filter(user_id=pay.processed_by_id, start_time__lte=pay.created_at)
            .filter(Q(end_time__isnull=True) | Q(end_time__gte=pay.created_at))
            .values_list('id', flat=True)
        )
    # A drawer's totals cover its whole shift, so with a date window only
    # shifts that lie entirely inside it can be compared with the window's
    # payments (a shift left open across days is reported elsewhere).
    drawer_sessions = sessions
    if since is not None:
        drawer_sessions = drawer_sessions.filter(start_time__date__gte=since)
    if until is not None:
        still_open_ok = until >= timezone.localdate()
        drawer_sessions = drawer_sessions.filter(
            Q(end_time__date__lte=until) | (Q(end_time__isnull=True) if still_open_ok else Q(pk__in=[]))
        )
    check_drawers(report, drawer_sessions, fix, include_closed, unplaceable_sessions)

    if include_customers:
        check_customers(report, fix, location)
    check_stock(report, location)
    return report
