"""
Delete test/dummy credit sales — every sale that still has a balance owing —
and undo everything they did, as if they never happened.

ONLY for debts that are fake (setup/training data). Real debts must be
settled or written off instead: deleting a real sale destroys its record.

For each sale with money still owing (COMPLETED / PARTIAL_REFUND, paid < bill):
  * un-refunded stock goes back to the batch it was sold from;
  * any money taken on it is taken back off the drawer totals it went into;
  * the customer's total spent / visit count are reduced;
  * the sale, its lines, payments and taxes are deleted.
Customers themselves are kept.

It is a dry run unless --commit is passed. Always read the dry run first.

    python manage.py clear_debtors
    python manage.py clear_debtors --commit
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.customers.models import Customer
from apps.inventory.models import StockBatch
from apps.sales.models import RegisterSession, Sale, SalePayment

ZERO = Decimal('0.00')
DRAWER_FIELDS = {'CASH': 'total_cash_sales', 'MOMO': 'total_momo_sales', 'CARD': 'total_card_sales'}


class Command(BaseCommand):
    help = ("Delete test/dummy sales that still have a balance owing, restoring stock, "
            "drawer totals and customer stats. Dry run unless --commit is passed.")

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true',
                            help="Actually delete. Without this the command only reports.")

    def handle(self, *args, **opts):
        commit = opts['commit']
        debts = (
            Sale.objects.filter(
                status__in=[Sale.Status.COMPLETED, Sale.Status.PARTIAL_REFUND],
                amount_paid__lt=F('total_amount'),
            )
            .select_related('customer', 'location')
            .order_by('created_at')
        )

        if not debts.exists():
            self.stdout.write(self.style.SUCCESS("No debtors. Nothing to do."))
            return

        with transaction.atomic():
            totals = {'sales': 0, 'owed': ZERO, 'paid': ZERO, 'units': 0}
            for sale in debts.select_for_update(of=('self',)):
                owed = sale.total_amount - sale.amount_paid
                units = self._undo(sale) if commit else sum(
                    i.quantity for i in sale.items.filter(is_refunded=False))
                who = sale.customer or 'no customer'
                self.stdout.write(
                    f"  {'DELETED' if commit else 'DELETE '} {sale.invoice_number}  "
                    f"{timezone.localtime(sale.created_at):%Y-%m-%d}  {sale.location.name}  {who}: "
                    f"bill {sale.total_amount}, paid {sale.amount_paid}, owed {owed}; "
                    f"{units} unit(s) back to stock"
                )
                totals['sales'] += 1
                totals['owed'] += owed
                totals['paid'] += sale.amount_paid
                totals['units'] += units

        self.stdout.write('')
        summary = (f"{totals['sales']} sale(s), {totals['owed']} owed, {totals['units']} unit(s) "
                   f"returned to stock")
        if totals['paid']:
            summary += f", {totals['paid']} already paid on them removed from the drawer totals"
        if commit:
            self.stdout.write(self.style.SUCCESS(f"Deleted {summary}."))
        else:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN: would delete {summary}. Re-run with --commit to apply."))
            self.stdout.write("Only do this if these debts are test data. Real debts should be settled.")

    def _undo(self, sale):
        """Reverse one sale's effects, then delete it. Returns units restocked."""
        # 1. Stock: lines not already refunded go back to their batch.
        units = 0
        for item in sale.items.filter(is_refunded=False):
            if item.source_batch_id:
                StockBatch.objects.filter(pk=item.source_batch_id).update(
                    quantity=F('quantity') + item.quantity)
            units += item.quantity

        # 2. Drawers: take back every payment row (incl. change/refunds, which
        #    are negative) from the drawer it went through.
        per_drawer = defaultdict(lambda: defaultdict(lambda: ZERO))
        for pay in sale.payments.all():
            session_id = pay.register_session_id or sale.register_session_id
            if session_id and pay.payment_method in DRAWER_FIELDS:
                per_drawer[session_id][pay.payment_method] += pay.amount
        for session_id, by_method in per_drawer.items():
            RegisterSession.objects.filter(pk=session_id).update(**{
                DRAWER_FIELDS[m]: F(DRAWER_FIELDS[m]) - amount for m, amount in by_method.items()
            })

        # 3. Customer stats.
        if sale.customer_id:
            Customer.objects.filter(pk=sale.customer_id).update(
                total_spent=F('total_spent') - sale.total_amount)
            Customer.objects.filter(pk=sale.customer_id, total_spent__lt=0).update(total_spent=ZERO)
            # total_visits is unsigned: never take it below zero.
            Customer.objects.filter(pk=sale.customer_id, total_visits__gt=0).update(
                total_visits=F('total_visits') - 1)

        # 4. The sale itself (lines, payments, taxes, delivery cascade).
        SalePayment.objects.filter(sale=sale).delete()
        sale.delete()
        return units
