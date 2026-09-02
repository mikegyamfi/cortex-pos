"""Tests for the `seed_products` management command and the shipped
`seed_data/products.json` catalogue (the K2 / Foxline proforma invoice)."""
import json
import os
from decimal import Decimal
from io import StringIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Category, Product, Unit

User = get_user_model()

SEED_FILE = os.path.join(
    settings.BASE_DIR, 'apps', 'products', 'seed_data', 'products.json'
)


def seed(*args, **kwargs):
    """Run the command, returning its stdout."""
    out = StringIO()
    call_command('seed_products', *args, stdout=out, **kwargs)
    return out.getvalue()


class SeedDataFileTests(TestCase):
    """The shipped catalogue file itself must stay sane."""

    @classmethod
    def setUpTestData(cls):
        with open(SEED_FILE, encoding='utf-8') as fh:
            cls.rows = json.load(fh)

    def test_file_has_rows(self):
        self.assertGreaterEqual(len(self.rows), 60)

    def test_every_row_has_the_required_keys(self):
        for row in self.rows:
            self.assertTrue(row.get('name'), row)
            self.assertTrue(row.get('sku'), row)
            self.assertIn('cost_price', row)
            self.assertIn('selling_price', row)

    def test_skus_are_unique(self):
        skus = [r['sku'] for r in self.rows]
        self.assertEqual(len(skus), len(set(skus)))

    def test_prices_are_positive_and_selling_covers_cost(self):
        for row in self.rows:
            cost = Decimal(str(row['cost_price']))
            sell = Decimal(str(row['selling_price']))
            self.assertGreater(cost, 0, row['sku'])
            self.assertGreaterEqual(sell, cost, row['sku'])

    def test_names_are_not_truncated_mid_word(self):
        """The invoice PDF clipped descriptions; the seed file must not."""
        for row in self.rows:
            self.assertFalse(row['name'].rstrip().endswith(','), row['name'])
            self.assertFalse(row['name'].rstrip().endswith('-'), row['name'])


class SeedProductsCommandTests(TestCase):

    def setUp(self):
        self.shop = Location.objects.create(
            name='Test Shop', location_type=Location.LocationType.SHOP, address='Accra'
        )

    # --- the shipped catalogue -------------------------------------------

    def test_seeds_catalogue_into_a_shop(self):
        with open(SEED_FILE, encoding='utf-8') as fh:
            rows = json.load(fh)

        out = seed('--file', SEED_FILE, '--location', 'Test Shop')

        self.assertEqual(Product.objects.count(), len(rows))
        self.assertEqual(
            Product.objects.filter(location=self.shop).count(), len(rows)
        )
        self.assertIn(f"Created {len(rows)} product(s)", out)
        self.assertIn("prices taken from the file", out)

    def test_products_land_with_zero_stock(self):
        seed('--file', SEED_FILE, '--location', 'Test Shop')
        self.assertEqual(StockBatch.objects.count(), 0)

    def test_prices_category_and_unit_come_from_the_file(self):
        seed('--file', SEED_FILE, '--location', 'Test Shop')

        p = Product.objects.get(location=self.shop, sku='R415')
        self.assertEqual(p.name, 'ERLA SATO 50ml GOOD MORNING')
        self.assertEqual(p.cost_price, Decimal('1.35'))
        self.assertEqual(p.selling_price, Decimal('1.45'))
        self.assertEqual(p.wholesale_price, Decimal('1.45'))
        self.assertEqual(p.tax_rate, Decimal('0.00'))
        self.assertEqual(p.category.name, 'General')
        self.assertEqual(p.unit.name, 'Piece')
        self.assertEqual(p.unit.symbol, 'pcs')

        # Only one generic category / unit is created for the whole run.
        self.assertEqual(Category.objects.count(), 1)
        self.assertEqual(Unit.objects.count(), 1)

    def test_slugs_are_generated_and_unique(self):
        seed('--file', SEED_FILE, '--location', 'Test Shop')
        slugs = list(Product.objects.values_list('slug', flat=True))
        self.assertTrue(all(slugs))
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_is_idempotent(self):
        seed('--file', SEED_FILE, '--location', 'Test Shop')
        total = Product.objects.count()

        out = seed('--file', SEED_FILE, '--location', 'Test Shop')

        self.assertEqual(Product.objects.count(), total)
        self.assertIn(f"Created 0 product(s); skipped {total} existing", out)

    def test_same_catalogue_can_be_seeded_into_two_shops(self):
        other = Location.objects.create(
            name='Second Shop', location_type=Location.LocationType.SHOP, address='Kumasi'
        )
        seed('--file', SEED_FILE, '--location', 'Test Shop')
        seed('--file', SEED_FILE, '--location', 'Second Shop')

        per_shop = Product.objects.filter(location=self.shop).count()
        self.assertEqual(Product.objects.filter(location=other).count(), per_shop)
        # SKUs are unique per shop, so R415 exists twice — once in each shop.
        self.assertEqual(Product.objects.filter(sku='R415').count(), 2)

    # --- flags ------------------------------------------------------------

    def test_zero_prices_flag_ignores_file_prices(self):
        out = seed('--file', SEED_FILE, '--location', 'Test Shop', '--zero-prices')

        self.assertIn("0 prices", out)
        self.assertFalse(
            Product.objects.exclude(
                cost_price=0, selling_price=0, wholesale_price=0
            ).exists()
        )

    def test_dry_run_writes_nothing(self):
        out = seed('--file', SEED_FILE, '--location', 'Test Shop', '--dry-run')

        self.assertIn('Would create', out)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(Category.objects.count(), 0)

    def test_category_flag_applies_when_the_row_has_none(self):
        path = self._write_csv(
            "name,sku\nWiper Blade,WB1\n"
        )
        seed('--file', path, '--location', 'Test Shop', '--category', 'Accessories')
        self.assertEqual(
            Product.objects.get(sku='WB1').category.name, 'Accessories'
        )

    def test_unknown_location_is_rejected(self):
        with self.assertRaises(CommandError):
            seed('--file', SEED_FILE, '--location', 'Nowhere')

    # --- CSV / price parsing ---------------------------------------------

    def test_csv_with_european_prices(self):
        path = self._write_csv(
            "Item Number,Item Description,Unit Price,List Price,UM\n"
            "R415,ERLA SATO 50ml,\"1,3500\",\"1,45000\",Piece\n"
            "W6810,Floor jack,\"1.234,50\",\"1.400,00\",Piece\n"
            "X1,Odd row,\"1,234\",abc,Piece\n"
        )
        seed('--file', path, '--location', 'Test Shop')

        erla = Product.objects.get(sku='R415')
        self.assertEqual(erla.cost_price, Decimal('1.35'))
        self.assertEqual(erla.selling_price, Decimal('1.45'))

        jack = Product.objects.get(sku='W6810')
        self.assertEqual(jack.cost_price, Decimal('1234.50'))
        self.assertEqual(jack.selling_price, Decimal('1400.00'))

        odd = Product.objects.get(sku='X1')
        self.assertEqual(odd.cost_price, Decimal('1234.00'))  # thousands separator
        self.assertEqual(odd.selling_price, Decimal('0.00'))  # unparseable -> 0

        # The header row must not become a product of its own.
        self.assertEqual(Product.objects.count(), 3)

    def test_headerless_csv_treats_the_first_column_as_the_name(self):
        path = self._write_csv("Wiper Blade\nOil Filter\n")
        seed('--file', path, '--location', 'Test Shop')

        self.assertEqual(Product.objects.count(), 2)
        self.assertTrue(Product.objects.filter(name='Wiper Blade').exists())

    def test_prices_default_to_zero_when_absent(self):
        path = self._write_csv("name,sku\nWiper Blade,WB1\n")
        out = seed('--file', path, '--location', 'Test Shop')

        p = Product.objects.get(sku='WB1')
        self.assertEqual(p.cost_price, Decimal('0.00'))
        self.assertEqual(p.selling_price, Decimal('0.00'))
        self.assertIn("0 prices", out)

    def test_negative_prices_are_clamped_to_zero(self):
        path = self._write_csv("name,sku,price\nWiper Blade,WB1,-5.00\n")
        seed('--file', path, '--location', 'Test Shop')
        self.assertEqual(Product.objects.get(sku='WB1').selling_price, Decimal('0.00'))

    # --- helpers ----------------------------------------------------------

    def _write_csv(self, content):
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.csv')
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:
            fh.write(content)
        self.addCleanup(os.remove, path)
        return path


class BarcodeAssignTests(TestCase):
    """The bulk 'scan a barcode onto each product' page."""

    def setUp(self):
        self.shop = Location.objects.create(
            name='K2 Shop', location_type=Location.LocationType.SHOP, address='Accra'
        )
        self.other_shop = Location.objects.create(
            name='Other Shop', location_type=Location.LocationType.SHOP, address='Tema'
        )
        self.manager = User.objects.create_user(
            username='manager', password='pw', role='MANAGER', assigned_location=self.shop
        )
        self.owner = User.objects.create_user(
            username='owner', password='pw', role='OWNER'
        )
        self.cashier = User.objects.create_user(
            username='cashier', password='pw', role='CASHIER', assigned_location=self.shop
        )
        self.p1 = self._product('K2 NIXO MIX', 'V130', self.shop)
        self.p2 = self._product('K2 POCO MIX', 'V350', self.shop)
        self.foreign = self._product('Someone Else Product', 'ZZZ1', self.other_shop)

        self.page_url = reverse('products:barcode_assign')
        self.save_url = reverse('products:barcode_assign_save')

    def _product(self, name, sku, location, **kwargs):
        return Product.objects.create(
            name=name, sku=sku, location=location,
            cost_price=Decimal('1.00'), selling_price=Decimal('2.00'), **kwargs
        )

    def _save(self, product, barcode):
        return self.client.post(
            self.save_url,
            data=json.dumps({'product_id': product.pk, 'barcode': barcode}),
            content_type='application/json',
        )

    # --- access -----------------------------------------------------------

    def test_page_requires_login(self):
        res = self.client.get(self.page_url)
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.url)

    def test_cashier_is_turned_away(self):
        self.client.force_login(self.cashier)
        res = self.client.get(self.page_url)
        self.assertRedirects(res, reverse('dashboard:index'), fetch_redirect_response=False)

    def test_cashier_cannot_save(self):
        self.client.force_login(self.cashier)
        res = self._save(self.p1, '5901528000000')
        self.assertEqual(res.status_code, 403)
        self.p1.refresh_from_db()
        self.assertIsNone(self.p1.barcode)

    def test_save_rejects_get(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.save_url).status_code, 405)

    # --- scoping ----------------------------------------------------------

    def test_manager_only_sees_their_own_shop(self):
        self.client.force_login(self.manager)
        res = self.client.get(self.page_url)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['total'], 2)
        skus = {r['sku'] for r in res.context['rows']}
        self.assertEqual(skus, {'V130', 'V350'})

    def test_product_names_cannot_break_out_of_the_script_tag(self):
        self._product('</script><script>alert(1)</script>', 'XSS1', self.shop)
        self.client.force_login(self.manager)

        html = self.client.get(self.page_url).content.decode()

        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('\\u003Cscript\\u003Ealert(1)', html)

    def test_manager_cannot_write_to_another_shops_product(self):
        self.client.force_login(self.manager)
        res = self._save(self.foreign, '5901528000000')

        self.assertEqual(res.status_code, 404)
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.barcode)

    def test_owner_picks_a_shop(self):
        self.client.force_login(self.owner)
        res = self.client.get(self.page_url, {'location': self.other_shop.pk})

        self.assertEqual(res.context['selected_location'], self.other_shop)
        self.assertEqual(res.context['total'], 1)

    def test_inactive_products_are_left_out(self):
        self._product('Retired', 'OLD1', self.shop, is_active=False)
        self.client.force_login(self.manager)
        res = self.client.get(self.page_url)
        self.assertEqual(res.context['total'], 2)

    # --- saving -----------------------------------------------------------

    def test_scan_assigns_the_barcode(self):
        self.client.force_login(self.manager)
        res = self._save(self.p1, '5901528000000')

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['barcode'], '5901528000000')
        self.assertEqual(body['done'], 1)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.barcode, '5901528000000')

    def test_duplicate_in_the_same_shop_is_rejected(self):
        self.p1.barcode = '5901528000000'
        self.p1.save()
        self.client.force_login(self.manager)

        res = self._save(self.p2, '5901528000000')

        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertFalse(body['ok'])
        self.assertIn('K2 NIXO MIX', body['error'])
        self.assertEqual(body['clash_id'], self.p1.pk)
        self.p2.refresh_from_db()
        self.assertIsNone(self.p2.barcode)

    def test_the_same_barcode_may_be_reused_in_another_shop(self):
        self.p1.barcode = '5901528000000'
        self.p1.save()
        self.client.force_login(self.owner)

        res = self._save(self.foreign, '5901528000000')

        self.assertEqual(res.status_code, 200)
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.barcode, '5901528000000')

    def test_rescanning_the_same_product_replaces_its_barcode(self):
        self.client.force_login(self.manager)
        self._save(self.p1, '1111111111111')
        res = self._save(self.p1, '2222222222222')

        self.assertEqual(res.status_code, 200)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.barcode, '2222222222222')

    def test_blank_clears_the_barcode_to_null(self):
        self.p1.barcode = '5901528000000'
        self.p1.save()
        self.client.force_login(self.manager)

        res = self._save(self.p1, '')

        self.assertEqual(res.status_code, 200)
        self.p1.refresh_from_db()
        # NULL, not '' — the unique constraint is conditional on NOT NULL, so
        # two blanks must never collide.
        self.assertIsNone(self.p1.barcode)

    def test_two_products_can_be_cleared_without_colliding(self):
        self.p1.barcode = '1111111111111'
        self.p1.save()
        self.p2.barcode = '2222222222222'
        self.p2.save()
        self.client.force_login(self.manager)

        self.assertEqual(self._save(self.p1, '').status_code, 200)
        self.assertEqual(self._save(self.p2, '').status_code, 200)

    def test_barcode_is_trimmed(self):
        self.client.force_login(self.manager)
        self._save(self.p1, '  5901528000000  ')
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.barcode, '5901528000000')

    def test_overlong_barcode_is_rejected(self):
        self.client.force_login(self.manager)
        res = self._save(self.p1, 'x' * 101)

        self.assertEqual(res.status_code, 400)
        self.p1.refresh_from_db()
        self.assertIsNone(self.p1.barcode)

    def test_malformed_body_is_rejected(self):
        self.client.force_login(self.manager)
        res = self.client.post(self.save_url, data='not json', content_type='application/json')
        self.assertEqual(res.status_code, 400)

    def test_non_numeric_product_id_is_rejected(self):
        self.client.force_login(self.manager)
        res = self.client.post(
            self.save_url,
            data=json.dumps({'product_id': 'abc', 'barcode': '123'}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 400)

    def test_junk_location_param_does_not_blow_up(self):
        self.client.force_login(self.owner)
        res = self.client.get(self.page_url, {'location': 'abc'})
        self.assertEqual(res.status_code, 200)

    def test_unknown_product_is_rejected(self):
        self.client.force_login(self.manager)
        res = self.client.post(
            self.save_url,
            data=json.dumps({'product_id': 999999, 'barcode': '123'}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 404)

    def test_save_only_touches_the_barcode(self):
        """update_fields must not let a stale price ride along."""
        self.client.force_login(self.manager)
        Product.objects.filter(pk=self.p1.pk).update(selling_price=Decimal('99.00'))

        self._save(self.p1, '5901528000000')

        self.p1.refresh_from_db()
        self.assertEqual(self.p1.selling_price, Decimal('99.00'))
        self.assertEqual(self.p1.barcode, '5901528000000')

    # --- the scanned code actually reaches the POS ------------------------

    def test_scanned_barcode_is_findable_by_the_pos_search(self):
        self.client.force_login(self.manager)
        self._save(self.p1, '5901528000000')

        self.client.force_login(self.cashier)
        res = self.client.get(reverse('sales:product_search'), {'q': '5901528000000'})

        self.assertEqual(res.status_code, 200)
        results = res.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['sku'], 'V130')
