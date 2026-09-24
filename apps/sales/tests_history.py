"""
Sales history page: single-day filtering, money totals, and confirmation that
selling really does reduce stock.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.sales.models import Sale, RegisterSession
from apps.users.models import User


class SaleHistoryTestBase(TestCase):
    def setUp(self):
        self.loc = Location.objects.create(name="History Shop", address="h")
        self.other = Location.objects.create(name="Second Shop", address="s")
        self.owner = User.objects.create_user(username="owner", password="pw", role="OWNER",
                                              assigned_location=self.loc)
        self.manager = User.objects.create_user(username="mgr", password="pw", role="MANAGER",
                                                assigned_location=self.loc)
        self.today = timezone.localdate()
        self.url = reverse('sales:list')

    def make_sale(self, total, paid, when=None, location=None):
        sale = Sale.objects.create(
            location=location or self.loc,
            cashier=self.manager,
            total_amount=Decimal(total),
            amount_paid=Decimal(paid),
            status=Sale.Status.COMPLETED,
        )
        if when is not None:
            # created_at is auto_now_add, so backdate with an update().
            Sale.objects.filter(pk=sale.pk).update(created_at=when)
            sale.refresh_from_db()
        return sale


class SaleHistoryDateFilterTests(SaleHistoryTestBase):

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.today_sale = self.make_sale("100.00", "100.00")
        self.yesterday_sale = self.make_sale("40.00", "40.00", when=now - timedelta(days=1))
        self.old_sale = self.make_sale("70.00", "70.00", when=now - timedelta(days=9))
        self.client.force_login(self.manager)

    def listed(self, res):
        return {s.pk for s in res.context['sales']}

    def test_defaults_to_today(self):
        res = self.client.get(self.url)
        self.assertEqual(self.listed(res), {self.today_sale.pk})
        self.assertEqual(res.context['filters']['date'], self.today.isoformat())

    def test_single_date_picks_exactly_that_day(self):
        yesterday = (self.today - timedelta(days=1)).isoformat()
        res = self.client.get(self.url, {'date': yesterday})
        self.assertEqual(self.listed(res), {self.yesterday_sale.pk})

    def test_a_day_with_no_trade_is_empty_not_an_error(self):
        res = self.client.get(self.url, {'date': (self.today - timedelta(days=4)).isoformat()})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.listed(res), set())
        self.assertEqual(res.context['summary']['billed'], Decimal('0.00'))
        self.assertEqual(res.context['summary']['count'], 0)

    def test_empty_date_shows_every_day(self):
        res = self.client.get(self.url, {'date': ''})
        self.assertEqual(self.listed(res),
                         {self.today_sale.pk, self.yesterday_sale.pk, self.old_sale.pk})

    def test_the_date_range_inputs_are_gone(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('name="start_date"', html)
        self.assertNotIn('name="end_date"', html)
        self.assertIn('name="date"', html)

    def test_the_date_input_is_prefilled_with_today(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'name="date" value="{self.today.isoformat()}"', html)

    def test_searching_looks_across_all_dates(self):
        """An invoice from last week must still be findable without first
        clearing the date."""
        res = self.client.get(self.url, {'q': self.old_sale.invoice_number})
        self.assertEqual(self.listed(res), {self.old_sale.pk})

    def test_search_can_still_be_pinned_to_a_day(self):
        res = self.client.get(self.url, {'q': self.old_sale.invoice_number,
                                         'date': self.today.isoformat()})
        self.assertEqual(self.listed(res), set())


class SaleHistoryTotalsTests(SaleHistoryTestBase):

    def setUp(self):
        super().setUp()
        self.make_sale("100.00", "100.00")            # paid in full
        self.make_sale("250.50", "200.00")            # part paid -> 50.50 owing
        self.make_sale("60.00", "0.00")               # unpaid
        self.make_sale("999.00", "999.00", when=timezone.now() - timedelta(days=3))  # other day
        self.client.force_login(self.manager)

    def test_totals_cover_the_selected_day_only(self):
        summary = self.client.get(self.url).context['summary']
        self.assertEqual(summary['billed'], Decimal('410.50'))       # 100 + 250.50 + 60
        self.assertEqual(summary['collected'], Decimal('300.00'))    # 100 + 200
        self.assertEqual(summary['outstanding'], Decimal('110.50'))  # 50.50 + 60
        self.assertEqual(summary['count'], 3)

    def test_totals_follow_the_date_filter(self):
        summary = self.client.get(
            self.url, {'date': (self.today - timedelta(days=3)).isoformat()}
        ).context['summary']
        self.assertEqual(summary['billed'], Decimal('999.00'))
        self.assertEqual(summary['count'], 1)

    def test_totals_cover_everything_when_the_date_is_cleared(self):
        summary = self.client.get(self.url, {'date': ''}).context['summary']
        self.assertEqual(summary['billed'], Decimal('1409.50'))
        self.assertEqual(summary['count'], 4)

    def test_totals_follow_the_status_filter(self):
        summary = self.client.get(self.url, {'status': 'DEBT'}).context['summary']
        self.assertEqual(summary['billed'], Decimal('310.50'))       # the two unpaid ones
        self.assertEqual(summary['outstanding'], Decimal('110.50'))
        self.assertEqual(summary['count'], 2)

    def test_totals_are_rendered_on_the_page(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('410.50', html)
        self.assertIn('Money Collected', html)
        self.assertIn('Outstanding', html)

    def test_staff_totals_never_include_another_shop(self):
        self.make_sale("5000.00", "5000.00", location=self.other)
        summary = self.client.get(self.url).context['summary']
        self.assertEqual(summary['billed'], Decimal('410.50'))

    def test_owner_totals_can_be_scoped_to_one_shop(self):
        self.make_sale("5000.00", "5000.00", location=self.other)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).context['summary']['billed'], Decimal('5410.50'))
        scoped = self.client.get(self.url, {'location': self.other.id}).context['summary']
        self.assertEqual(scoped['billed'], Decimal('5000.00'))


class SellingReducesStockTests(TestCase):
    """Confirm that completing a sale takes the quantity out of stock."""

    def setUp(self):
        self.loc = Location.objects.create(name="Till Shop", address="t")
        self.cashier = User.objects.create_user(username="till", password="pw", role="CASHIER",
                                                assigned_location=self.loc)
        self.product = Product.objects.create(name="Wiper Blade", slug="wiper", sku="WIP-1",
                                              location=self.loc, cost_price=Decimal("6.00"),
                                              selling_price=Decimal("10.00"))
        # Two batches so FEFO ordering is exercised.
        self.old_batch = StockBatch.objects.create(
            product=self.product, location=self.loc, quantity=5, cost_price=Decimal("6.00"),
            expiry_date=timezone.localdate() + timedelta(days=10),
        )
        self.new_batch = StockBatch.objects.create(
            product=self.product, location=self.loc, quantity=20, cost_price=Decimal("6.50"),
            expiry_date=timezone.localdate() + timedelta(days=200),
        )
        RegisterSession.objects.create(user=self.cashier, location=self.loc,
                                       opening_balance=Decimal("0.00"),
                                       status=RegisterSession.Status.OPEN)
        self.client.force_login(self.cashier)

    def sell(self, qty, amount):
        return self.client.post(
            reverse("sales:process_sale"),
            data=json.dumps({
                "cart": [{"id": self.product.id, "qty": qty, "price": "10.00"}],
                "payments": [{"method": "CASH", "amount": amount}],
                "customer_id": None,
            }),
            content_type="application/json",
        )

    def on_hand(self):
        return sum(b.quantity for b in StockBatch.objects.filter(product=self.product))

    def test_selling_reduces_quantity_on_hand(self):
        self.assertEqual(self.on_hand(), 25)
        body = self.sell(3, "30.00").json()
        self.assertTrue(body["success"], body)
        self.assertEqual(self.on_hand(), 22)

    def test_stock_comes_out_of_the_soonest_expiring_batch_first(self):
        self.sell(4, "40.00")
        self.old_batch.refresh_from_db()
        self.new_batch.refresh_from_db()
        self.assertEqual(self.old_batch.quantity, 1)   # drained first
        self.assertEqual(self.new_batch.quantity, 20)  # untouched

    def test_a_sale_spanning_two_batches_deducts_from_both(self):
        self.sell(8, "80.00")
        self.old_batch.refresh_from_db()
        self.new_batch.refresh_from_db()
        self.assertEqual(self.old_batch.quantity, 0)
        self.assertEqual(self.new_batch.quantity, 17)
        self.assertEqual(self.on_hand(), 17)

    def test_successive_sales_keep_reducing_stock(self):
        for _ in range(3):
            self.sell(2, "20.00")
        self.assertEqual(self.on_hand(), 19)

    def test_overselling_is_refused_and_stock_is_untouched(self):
        res = self.sell(26, "260.00")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Insufficient stock", res.json()["message"])
        self.assertEqual(self.on_hand(), 25)

    def test_sale_items_record_the_batch_they_came_from(self):
        body = self.sell(6, "60.00").json()
        sale = Sale.objects.get(id=body["sale_id"])
        taken = {item.source_batch_id: item.quantity for item in sale.items.all()}
        self.assertEqual(taken, {self.old_batch.id: 5, self.new_batch.id: 1})

    def test_stock_levels_page_reflects_the_sale(self):
        self.sell(24, "240.00")
        self.client.force_login(User.objects.create_user(
            username="invmgr", password="pw", role="MANAGER", assigned_location=self.loc))
        res = self.client.get(reverse('inventory:dashboard'))
        self.assertEqual(sum(b.quantity for b in res.context['batches']), 1)
        self.assertEqual(res.context['low_stock_count'], 1)  # 1 left, under threshold
