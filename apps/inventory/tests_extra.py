"""
Receiving + stock-levels search coverage.

Split from tests.py to keep the one-step receiving flow tests and these
UI-facing tests readable side by side; the runner picks up both.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.inventory.models import StockBatch, StockTransfer
from apps.location.models import Location
from apps.products.models import Product

User = get_user_model()


class AdminOneStepReceivingTests(TestCase):
    """An owner/admin must be able to receive stock and confirm it in one go."""

    def setUp(self):
        self.warehouse = Location.objects.create(name="Central Warehouse", location_type='WAREHOUSE', address="w")
        self.shop_a = Location.objects.create(name="Shop A", location_type='SHOP', address="a")
        self.shop_b = Location.objects.create(name="Shop B", location_type='SHOP', address="b")

        self.owner = User.objects.create_user(username='own', password='pw', role='OWNER')
        self.client.force_login(self.owner)

        self.p_a = Product.objects.create(name="Alternator", slug="alt-a", sku="ALT-9", location=self.shop_a,
                                          cost_price=Decimal('100'), selling_price=Decimal('150'))
        self.p_wh = Product.objects.create(name="Alternator", slug="alt-w", sku="ALT-9", location=self.warehouse,
                                           cost_price=Decimal('90'), selling_price=Decimal('150'))
        StockBatch.objects.create(product=self.p_wh, location=self.warehouse, quantity=40, cost_price=Decimal('90'))

    def post_receive(self, **overrides):
        payload = {
            'location': self.shop_a.id, 'product': self.p_a.id, 'quantity': 10,
            'cost_price': '', 'supplier': '', 'batch_number': '',
            'expiry_date': '', 'manufactured_date': '', 'source_location': '',
        }
        payload.update(overrides)
        return self.client.post(reverse('inventory:receive_stock'), payload)

    def qty_at(self, location):
        return sum(b.quantity for b in StockBatch.objects.filter(location=location))

    def test_owner_receives_into_any_shop_in_one_request(self):
        res = self.post_receive(location=self.shop_b.id, quantity=5)
        self.assertRedirects(res, reverse('inventory:dashboard'))
        # Landed in the shop the owner chose — no second confirmation step.
        self.assertEqual(self.qty_at(self.shop_b), 5)
        self.assertEqual(self.qty_at(self.shop_a), 0)

    def test_owner_receives_from_warehouse_moves_and_confirms_in_one_request(self):
        res = self.post_receive(quantity=10, source_location=self.warehouse.id)
        self.assertRedirects(res, reverse('inventory:dashboard'))

        self.assertEqual(self.qty_at(self.shop_a), 10)     # added here
        self.assertEqual(self.qty_at(self.warehouse), 30)  # removed there

        transfer = StockTransfer.objects.get()
        self.assertEqual(transfer.status, StockTransfer.Status.RECEIVED)
        self.assertEqual(transfer.received_by, self.owner)
        self.assertEqual(transfer.approved_by, self.owner)
        # Nothing left waiting on a second person.
        self.assertFalse(StockTransfer.objects.exclude(status=StockTransfer.Status.RECEIVED).exists())

    def test_repeated_receipts_accumulate(self):
        self.post_receive(quantity=4, source_location=self.warehouse.id)
        self.post_receive(quantity=6, source_location=self.warehouse.id)
        self.assertEqual(self.qty_at(self.shop_a), 10)
        self.assertEqual(self.qty_at(self.warehouse), 30)
        self.assertEqual(StockTransfer.objects.count(), 2)

    def test_cannot_receive_from_the_same_location(self):
        res = self.post_receive(source_location=self.shop_a.id)
        self.assertEqual(res.status_code, 200)
        self.assertFormError(res.context['form'], 'source_location',
                             'Source and destination cannot be the same location.')
        self.assertEqual(self.qty_at(self.shop_a), 0)

    def test_cost_price_defaults_to_product_cost_when_left_blank(self):
        self.post_receive(quantity=3)
        self.assertEqual(StockBatch.objects.get(location=self.shop_a).cost_price, Decimal('100'))

    def test_explicit_cost_price_is_respected(self):
        self.post_receive(quantity=3, cost_price='88.50')
        self.assertEqual(StockBatch.objects.get(location=self.shop_a).cost_price, Decimal('88.50'))

    def test_owner_sees_every_shops_products_and_all_source_locations(self):
        res = self.client.get(reverse('inventory:receive_stock'))
        form = res.context['form']
        self.assertIn('location', form.fields)
        self.assertCountEqual(list(form.fields['product'].queryset), [self.p_a, self.p_wh])
        self.assertCountEqual(list(form.fields['source_location'].queryset),
                              [self.warehouse, self.shop_a, self.shop_b])

    def test_product_dropdown_is_marked_searchable(self):
        """The searchable-select JS keys off this class; without it the dropdown
        is a plain scroll-through list."""
        html = self.client.get(reverse('inventory:receive_stock')).content.decode()
        self.assertRegex(html, r'<select[^>]*name="product"[^>]*class="[^"]*select2')

    def test_searchable_select_assets_are_loaded_on_every_page(self):
        html = self.client.get(reverse('inventory:receive_stock')).content.decode()
        self.assertIn('searchable-select.js', html)
        self.assertIn('searchable-select.css', html)


class StockLevelsSearchTests(TestCase):
    """The stock levels page must let staff search for a product."""

    def setUp(self):
        self.shop = Location.objects.create(name="Search Shop", address="s")
        self.other = Location.objects.create(name="Other Shop", address="o")
        self.manager = User.objects.create_user(username='m', password='pw', role='MANAGER',
                                                assigned_location=self.shop)
        self.owner = User.objects.create_user(username='o', password='pw', role='OWNER')

        def product(name, sku, barcode=None, location=None, threshold=10):
            loc = location or self.shop
            return Product.objects.create(
                name=name, slug=f"{sku.lower()}-{loc.id}", sku=sku, barcode=barcode, location=loc,
                cost_price=Decimal('5'), selling_price=Decimal('9'), low_stock_threshold=threshold,
            )

        self.brake = product("Brake Pad Front", "BRK-100", "5060000000011")
        self.oil = product("Engine Oil 5W30", "OIL-200", "5060000000028")
        self.plug = product("Spark Plug", "SPK-300")       # no stock at all
        self.filter = product("Oil Filter", "FLT-400")     # below threshold
        self.foreign = product("Brake Pad Rear", "BRK-900", location=self.other)

        StockBatch.objects.create(product=self.brake, location=self.shop, quantity=25,
                                  cost_price=Decimal('5'), batch_number="LOT-AAA")
        StockBatch.objects.create(product=self.oil, location=self.shop, quantity=60, cost_price=Decimal('5'))
        StockBatch.objects.create(product=self.filter, location=self.shop, quantity=3, cost_price=Decimal('5'))
        StockBatch.objects.create(product=self.foreign, location=self.other, quantity=99, cost_price=Decimal('5'))

        self.client.force_login(self.manager)
        self.url = reverse('inventory:dashboard')

    def batch_products(self, res):
        return sorted({b.product.name for b in res.context['batches']})

    def test_search_by_product_name(self):
        res = self.client.get(self.url, {'q': 'brake'})
        self.assertEqual(self.batch_products(res), ["Brake Pad Front"])

    def test_search_is_case_insensitive_and_partial(self):
        for term in ('BRAKE', 'brake pad', 'ake Pad F'):
            res = self.client.get(self.url, {'q': term})
            self.assertEqual(self.batch_products(res), ["Brake Pad Front"], term)

    def test_search_by_sku(self):
        res = self.client.get(self.url, {'q': 'OIL-200'})
        self.assertEqual(self.batch_products(res), ["Engine Oil 5W30"])

    def test_search_by_barcode(self):
        res = self.client.get(self.url, {'q': '5060000000011'})
        self.assertEqual(self.batch_products(res), ["Brake Pad Front"])

    def test_search_by_batch_number(self):
        res = self.client.get(self.url, {'q': 'LOT-AAA'})
        self.assertEqual(self.batch_products(res), ["Brake Pad Front"])

    def test_search_with_no_match_returns_nothing(self):
        res = self.client.get(self.url, {'q': 'gearbox'})
        self.assertEqual(self.batch_products(res), [])
        self.assertContains(res, 'No stock matching')

    def test_blank_search_shows_everything(self):
        res = self.client.get(self.url, {'q': '   '})
        self.assertEqual(self.batch_products(res),
                         ["Brake Pad Front", "Engine Oil 5W30", "Oil Filter"])

    def test_search_never_crosses_into_another_shop(self):
        res = self.client.get(self.url, {'q': 'brake'})
        self.assertEqual(self.batch_products(res), ["Brake Pad Front"])
        self.assertNotContains(res, "BRK-900")

    def test_search_box_is_rendered_and_keeps_the_term(self):
        res = self.client.get(self.url, {'q': 'brake'})
        self.assertContains(res, 'name="q"')
        self.assertContains(res, 'value="brake"')

    def test_owner_search_spans_all_shops(self):
        self.client.force_login(self.owner)
        res = self.client.get(self.url, {'q': 'brake'})
        self.assertEqual(self.batch_products(res), ["Brake Pad Front", "Brake Pad Rear"])

    def test_owner_search_can_be_narrowed_to_one_shop(self):
        self.client.force_login(self.owner)
        res = self.client.get(self.url, {'q': 'brake', 'location': self.other.id})
        self.assertEqual(self.batch_products(res), ["Brake Pad Rear"])


class StockStatusCardTests(StockLevelsSearchTests):
    """Out-of-stock / low-stock / in-stock summary cards."""

    def test_counts_reflect_the_shop(self):
        res = self.client.get(self.url)
        self.assertEqual(res.context['in_stock_count'], 3)       # brake, oil, filter
        self.assertEqual(res.context['out_of_stock_count'], 1)   # spark plug
        self.assertEqual(res.context['low_stock_count'], 1)      # oil filter (3 <= 10)

    def test_counts_do_not_move_while_searching(self):
        res = self.client.get(self.url, {'q': 'brake'})
        self.assertEqual(res.context['in_stock_count'], 3)
        self.assertEqual(res.context['out_of_stock_count'], 1)
        self.assertEqual(res.context['low_stock_count'], 1)

    def test_out_of_stock_view_lists_the_products_to_reorder(self):
        res = self.client.get(self.url, {'stock': 'out'})
        self.assertEqual([p.name for p in res.context['product_rows']], ["Spark Plug"])
        self.assertContains(res, "Out of Stock")

    def test_low_stock_view_lists_products_with_quantity_on_hand(self):
        res = self.client.get(self.url, {'stock': 'low'})
        rows = res.context['product_rows']
        self.assertEqual([p.name for p in rows], ["Oil Filter"])
        self.assertEqual(rows[0].on_hand, 3)

    def test_out_of_stock_view_can_be_searched(self):
        res = self.client.get(self.url, {'stock': 'out', 'q': 'spark'})
        self.assertEqual([p.name for p in res.context['product_rows']], ["Spark Plug"])
        res = self.client.get(self.url, {'stock': 'out', 'q': 'brake'})
        self.assertEqual(list(res.context['product_rows']), [])

    def test_default_view_shows_batches_not_products(self):
        res = self.client.get(self.url)
        self.assertIsNone(res.context['product_rows'])

    def test_selling_out_moves_a_product_into_the_out_of_stock_card(self):
        StockBatch.objects.filter(product=self.filter).update(quantity=0)
        res = self.client.get(self.url)
        self.assertEqual(res.context['out_of_stock_count'], 2)
        self.assertEqual(res.context['low_stock_count'], 0)

    def test_receiving_clears_a_product_out_of_the_out_of_stock_card(self):
        StockBatch.objects.create(product=self.plug, location=self.shop, quantity=40, cost_price=Decimal('5'))
        res = self.client.get(self.url)
        self.assertEqual(res.context['out_of_stock_count'], 0)
        self.assertEqual(res.context['in_stock_count'], 4)

    def test_staff_counts_exclude_other_shops(self):
        out = self.client.get(self.url, {'stock': 'out'}).context['product_rows']
        self.assertNotIn("Brake Pad Rear", [p.name for p in out])
        self.assertEqual(self.client.get(self.url).context['in_stock_count'], 3)
