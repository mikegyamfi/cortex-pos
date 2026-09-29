"""Tests for the dedupe_stock_batches management command."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from apps.inventory.models import StockAdjustment, StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.sales.models import Sale, SaleItem
from apps.users.models import User


class DedupeStockBatchesTests(TestCase):
    def setUp(self):
        self.shop = Location.objects.create(name="Dedupe Shop", address="d")
        self.other = Location.objects.create(name="Dedupe Two", address="e")
        self.user = User.objects.create_user(username="dedupe", password="pw", role="OWNER")
        self.product = Product.objects.create(name="Clutch Cable", slug="clutch", sku="CL-1",
                                              location=self.shop, cost_price=Decimal('20'),
                                              selling_price=Decimal('35'))
        self.now = timezone.now()

    def batch(self, qty, minutes_ago=0, days_ago=0, location=None, cost='20', expiry=None):
        b = StockBatch.objects.create(
            product=self.product, location=location or self.shop, quantity=qty,
            cost_price=Decimal(cost), expiry_date=expiry,
        )
        # received_date has a default, so set the moment explicitly.
        when = self.now - timedelta(days=days_ago, minutes=minutes_ago)
        StockBatch.objects.filter(pk=b.pk).update(received_date=when)
        b.refresh_from_db()
        return b

    def run_cmd(self, *args):
        out = StringIO()
        call_command('dedupe_stock_batches', *args, stdout=out, stderr=out)
        return out.getvalue()

    def on_hand(self, location=None):
        return sum(b.quantity for b in StockBatch.objects.filter(location=location or self.shop))

    # ---------------------------------------------------------------- dry run

    def test_dry_run_changes_nothing(self):
        self.batch(5, minutes_ago=30)
        self.batch(1, minutes_ago=10)
        out = self.run_cmd()
        self.assertIn('DRY RUN', out)
        self.assertEqual(StockBatch.objects.count(), 2)
        self.assertEqual(self.on_hand(), 6)

    def test_dry_run_reports_what_would_happen(self):
        self.batch(5, minutes_ago=30)
        self.batch(1, minutes_ago=10)
        out = self.run_cmd()
        self.assertIn('Clutch Cable (CL-1)', out)
        self.assertIn('on hand 6 -> 5', out)
        self.assertIn('1 phantom unit', out)

    def test_nothing_to_do_is_reported_cleanly(self):
        self.batch(5)
        out = self.run_cmd()
        self.assertIn('Nothing to do', out)

    # ---------------------------------------------------------------- delete

    def test_commit_keeps_the_oldest_and_deletes_the_rest(self):
        keeper = self.batch(5, minutes_ago=30)
        self.batch(1, minutes_ago=20)
        self.batch(1, minutes_ago=10)

        self.run_cmd('--commit')

        remaining = list(StockBatch.objects.all())
        self.assertEqual([b.id for b in remaining], [keeper.id])
        self.assertEqual(remaining[0].quantity, 5)   # untouched, not summed
        self.assertEqual(self.on_hand(), 5)

    def test_oldest_is_by_received_date_not_insertion_order(self):
        newer = self.batch(9, minutes_ago=5)
        older = self.batch(4, minutes_ago=90)
        self.run_cmd('--commit')
        self.assertEqual([b.id for b in StockBatch.objects.all()], [older.id])
        self.assertFalse(StockBatch.objects.filter(pk=newer.pk).exists())

    def test_products_are_deduped_independently(self):
        second = Product.objects.create(name="Throttle Cable", slug="throttle", sku="TH-1",
                                        location=self.shop, cost_price=Decimal('8'),
                                        selling_price=Decimal('14'))
        self.batch(5, minutes_ago=30)
        self.batch(2, minutes_ago=10)
        StockBatch.objects.create(product=second, location=self.shop, quantity=7,
                                  cost_price=Decimal('8'))
        self.run_cmd('--commit')
        self.assertEqual(self.on_hand(), 12)  # 5 kept + the untouched 7
        self.assertEqual(StockBatch.objects.filter(product=second).count(), 1)

    def test_same_product_at_two_locations_is_not_a_duplicate(self):
        self.batch(5, minutes_ago=30)
        self.batch(3, minutes_ago=10, location=self.other)
        out = self.run_cmd()
        self.assertIn('Nothing to do', out)

    # ----------------------------------------------------- protected history

    def test_a_batch_with_sales_history_can_still_be_removed(self):
        keeper = self.batch(5, minutes_ago=30)
        dupe = self.batch(4, minutes_ago=10)
        sale = Sale.objects.create(location=self.shop, cashier=self.user,
                                   total_amount=Decimal('35'), amount_paid=Decimal('35'),
                                   status=Sale.Status.COMPLETED)
        item = SaleItem.objects.create(sale=sale, product=self.product, source_batch=dupe,
                                       quantity=1, unit_price=Decimal('35'),
                                       unit_cost=Decimal('20'), total_price=Decimal('35'))
        adjustment = StockAdjustment.objects.create(
            location=self.shop, batch=dupe, adjusted_quantity=-1, reason='DAMAGE',
            notes='test', performed_by=self.user,
        )

        out = self.run_cmd('--commit')

        self.assertFalse(StockBatch.objects.filter(pk=dupe.pk).exists())
        item.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(item.source_batch_id, keeper.id)      # history preserved
        self.assertEqual(adjustment.batch_id, keeper.id)
        self.assertIn('Repointed 1 sale item(s) and 1 adjustment(s)', out)

    # ---------------------------------------------------------- --same-day

    def test_same_day_leaves_genuine_restocks_from_other_days_alone(self):
        self.batch(5, days_ago=6)
        self.batch(4, days_ago=0, minutes_ago=30)
        out = self.run_cmd('--same-day')
        self.assertIn('Nothing to do', out)

    def test_same_day_removes_only_the_repeats_from_that_day(self):
        old = self.batch(5, days_ago=6)
        today_keeper = self.batch(4, minutes_ago=40)
        self.batch(4, minutes_ago=20)
        self.batch(4, minutes_ago=5)

        self.run_cmd('--same-day', '--commit')

        self.assertCountEqual([b.id for b in StockBatch.objects.all()], [old.id, today_keeper.id])
        self.assertEqual(self.on_hand(), 9)   # 5 from before + one 4 from today

    # ------------------------------------------------------------- scoping

    def test_location_scope(self):
        self.batch(5, minutes_ago=30)
        self.batch(1, minutes_ago=10)
        other_a = self.batch(3, minutes_ago=30, location=self.other)
        other_b = self.batch(3, minutes_ago=10, location=self.other)

        self.run_cmd('--location', 'Dedupe Two', '--commit')

        self.assertEqual(StockBatch.objects.filter(location=self.shop).count(), 2)  # untouched
        self.assertEqual([b.id for b in StockBatch.objects.filter(location=self.other)], [other_a.id])

    def test_location_scope_is_case_insensitive(self):
        self.batch(3, minutes_ago=30, location=self.other)
        self.batch(3, minutes_ago=10, location=self.other)
        self.run_cmd('--location', 'dedupe two', '--commit')
        self.assertEqual(StockBatch.objects.filter(location=self.other).count(), 1)

    def test_unknown_location_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_cmd('--location', 'Nowhere')

    def test_sku_scope(self):
        second = Product.objects.create(name="Throttle Cable", slug="throttle2", sku="TH-1",
                                        location=self.shop, cost_price=Decimal('8'),
                                        selling_price=Decimal('14'))
        self.batch(5, minutes_ago=30)
        self.batch(1, minutes_ago=10)
        for _ in range(2):
            StockBatch.objects.create(product=second, location=self.shop, quantity=7,
                                      cost_price=Decimal('8'))

        self.run_cmd('--sku', 'TH-1', '--commit')

        self.assertEqual(StockBatch.objects.filter(product=self.product).count(), 2)  # untouched
        self.assertEqual(StockBatch.objects.filter(product=second).count(), 1)

    def test_since_window(self):
        old_a = self.batch(5, days_ago=30)
        old_b = self.batch(5, days_ago=29)
        recent_keeper = self.batch(2, days_ago=1)
        self.batch(2, minutes_ago=5)

        cutoff = (timezone.localdate() - timedelta(days=2)).isoformat()
        self.run_cmd('--since', cutoff, '--commit')

        ids = [b.id for b in StockBatch.objects.order_by('received_date')]
        self.assertEqual(ids, [old_a.id, old_b.id, recent_keeper.id])

    def test_bad_date_is_an_error(self):
        self.batch(5, minutes_ago=30)
        self.batch(5, minutes_ago=10)
        with self.assertRaises(CommandError):
            self.run_cmd('--since', 'last-tuesday')

    # --------------------------------------------------------------- --merge

    def test_merge_mode_sums_instead_of_dropping(self):
        keeper = self.batch(5, minutes_ago=30, cost='20')
        self.batch(5, minutes_ago=10, cost='30')

        self.run_cmd('--merge', '--commit')

        keeper.refresh_from_db()
        self.assertEqual(StockBatch.objects.count(), 1)
        self.assertEqual(keeper.quantity, 10)
        self.assertEqual(keeper.cost_price, Decimal('25.00'))  # weighted average keeps the value
        self.assertEqual(self.on_hand(), 10)

    def test_merge_keeps_the_earliest_expiry(self):
        today = timezone.localdate()
        keeper = self.batch(5, minutes_ago=30, expiry=today + timedelta(days=200))
        self.batch(5, minutes_ago=10, expiry=today + timedelta(days=30))
        self.run_cmd('--merge', '--commit')
        keeper.refresh_from_db()
        self.assertEqual(keeper.expiry_date, today + timedelta(days=30))

    # --------------------------------------------------------------- warning

    def test_warns_when_the_kept_batch_is_empty(self):
        self.batch(0, minutes_ago=30)
        self.batch(8, minutes_ago=10)
        out = self.run_cmd()
        self.assertIn('the kept batch is empty', out)
        self.assertIn('will read 0', out)
