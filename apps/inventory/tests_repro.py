"""
Stock Levels must agree with the POS.

Reported symptom: an admin received stock, the POS showed the new quantity, but
Stock Levels "did not increase". Cause: every receipt creates a new StockBatch
and the page listed batches one per row, so the total was never shown in one
place. Stock on hand is now grouped per product.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.sales.views import _location_stock_map
from apps.users.models import User


class StockLevelsMatchThePOSTests(TestCase):
    def setUp(self):
        self.shop = Location.objects.create(name="Repro Shop", address="r")
        self.warehouse = Location.objects.create(name="Repro Warehouse",
                                                 location_type='WAREHOUSE', address="w")
        self.manager = User.objects.create_user(username="repromgr", password="pw", role="MANAGER",
                                                assigned_location=self.shop)
        self.product = Product.objects.create(name="Fan Belt", slug="fanbelt", sku="FB-1",
                                              location=self.shop, cost_price=Decimal('10'),
                                              selling_price=Decimal('20'), low_stock_threshold=5)
        # Opening stock, as if it had been there a while.
        StockBatch.objects.create(product=self.product, location=self.shop, quantity=5,
                                  cost_price=Decimal('10'))
        self.client.force_login(self.manager)
        self.url = reverse('inventory:dashboard')

    def receive(self, qty, **extra):
        payload = {
            'product': self.product.id, 'quantity': qty,
            'cost_price': '', 'supplier': '', 'batch_number': '',
            'expiry_date': '', 'manufactured_date': '', 'source_location': '',
        }
        payload.update(extra)
        return self.client.post(reverse('inventory:receive_stock'), payload)

    def stock_row(self, res, product=None):
        product = product or self.product
        rows = [r for r in res.context['stock_rows'] if r['product'].id == product.id]
        return rows[0] if rows else None

    def pos_qty(self):
        return _location_stock_map(self.shop).get(self.product.id, 0)

    def test_receiving_increases_the_quantity_shown_on_stock_levels(self):
        self.receive(20)
        row = self.stock_row(self.client.get(self.url))
        self.assertEqual(row['quantity'], 25)
        self.assertEqual(row['quantity'], self.pos_qty())

    def test_one_row_per_product_no_matter_how_many_receipts(self):
        for qty in (20, 7, 3):
            self.receive(qty)
        res = self.client.get(self.url)
        rows = [r for r in res.context['stock_rows'] if r['product'].id == self.product.id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quantity'], 35)
        self.assertEqual(rows[0]['quantity'], self.pos_qty())
        # The batches are still there, as the breakdown behind the total.
        self.assertEqual(len(rows[0]['batches']), 4)

    def test_the_total_is_rendered_on_the_page(self):
        import re
        self.receive(20)
        html = self.client.get(self.url).content.decode()
        # The quantity badge for the product must read the combined 25.
        self.assertRegex(html, r'class="badge bg-label-\w+ fs-6">\s*25\s*<')
        self.assertNotRegex(html, r'class="badge bg-label-\w+ fs-6">\s*20\s*<')

    def test_row_value_sums_the_batches_at_their_own_costs(self):
        self.receive(10, cost_price='12.00')
        row = self.stock_row(self.client.get(self.url))
        self.assertEqual(row['value'], Decimal('170.00'))  # 5x10 + 10x12

    def test_next_expiry_is_the_soonest_of_the_batches(self):
        self.receive(10, expiry_date='2027-01-31')
        self.receive(10, expiry_date='2026-11-30')
        row = self.stock_row(self.client.get(self.url))
        self.assertEqual(row['expiry_date'].isoformat(), '2026-11-30')
        self.assertEqual(row['quantity'], 25)

    def test_receiving_from_a_warehouse_also_shows_the_new_total(self):
        twin = Product.objects.create(name="Fan Belt", slug="fanbelt-wh", sku="FB-1",
                                      location=self.warehouse, cost_price=Decimal('9'),
                                      selling_price=Decimal('20'))
        StockBatch.objects.create(product=twin, location=self.warehouse, quantity=50,
                                  cost_price=Decimal('9'))
        self.receive(30, source_location=self.warehouse.id)

        row = self.stock_row(self.client.get(self.url))
        self.assertEqual(row['quantity'], 35)
        self.assertEqual(row['quantity'], self.pos_qty())

    def test_selling_reduces_the_same_total(self):
        self.receive(20)
        StockBatch.objects.filter(product=self.product).order_by('-quantity').update(quantity=8)
        row = self.stock_row(self.client.get(self.url))
        self.assertEqual(row['quantity'], self.pos_qty())

    def test_low_stock_colour_uses_the_product_total_not_a_single_batch(self):
        """Three batches of 2 is 6 units — above the threshold of 5 — so the row
        must not be flagged low just because one batch is small."""
        StockBatch.objects.filter(product=self.product).delete()
        for _ in range(3):
            StockBatch.objects.create(product=self.product, location=self.shop, quantity=2,
                                      cost_price=Decimal('10'))
        res = self.client.get(self.url)
        self.assertEqual(self.stock_row(res)['quantity'], 6)
        self.assertEqual(res.context['low_stock_count'], 0)

    def test_owner_sees_the_same_product_split_per_location(self):
        owner = User.objects.create_user(username="reproowner", password="pw", role="OWNER")
        twin = Product.objects.create(name="Fan Belt", slug="fanbelt-wh2", sku="FB-1",
                                      location=self.warehouse, cost_price=Decimal('9'),
                                      selling_price=Decimal('20'))
        StockBatch.objects.create(product=twin, location=self.warehouse, quantity=50,
                                  cost_price=Decimal('9'))
        self.client.force_login(owner)
        rows = self.client.get(self.url).context['stock_rows']
        by_location = {(r['product'].id, r['location'].id): r['quantity'] for r in rows}
        self.assertEqual(by_location[(self.product.id, self.shop.id)], 5)
        self.assertEqual(by_location[(twin.id, self.warehouse.id)], 50)

    def test_search_still_works_against_the_grouped_rows(self):
        self.receive(20)
        res = self.client.get(self.url, {'q': 'FB-1'})
        self.assertEqual(self.stock_row(res)['quantity'], 25)
        res = self.client.get(self.url, {'q': 'nothing-like-this'})
        self.assertEqual(res.context['stock_rows'], [])

    def test_expiry_warning_covers_the_shop_not_just_the_search(self):
        """The banner is a shop-wide warning, like the summary cards — it must
        not disappear because someone searched for an unrelated product."""
        from datetime import timedelta
        from django.utils import timezone

        soon = timezone.localdate() + timedelta(days=7)
        StockBatch.objects.create(product=self.product, location=self.shop, quantity=4,
                                  cost_price=Decimal('10'), expiry_date=soon)
        other = Product.objects.create(name="Radiator Cap", slug="radcap", sku="RC-1",
                                       location=self.shop, cost_price=Decimal('3'),
                                       selling_price=Decimal('6'))
        StockBatch.objects.create(product=other, location=self.shop, quantity=10,
                                  cost_price=Decimal('3'))

        self.assertEqual(len(self.client.get(self.url).context['expiring_soon']), 1)
        res = self.client.get(self.url, {'q': 'Radiator'})
        self.assertEqual(len(res.context['expiring_soon']), 1)
        self.assertEqual(len(res.context['stock_rows']), 1)
