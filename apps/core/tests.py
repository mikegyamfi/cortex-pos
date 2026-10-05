"""
Cross-app checks for the site-wide search behaviour: every list page must load
the all-column table search and the type-to-search dropdown enhancement.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.customers.models import Customer
from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.users.models import User


class SearchAssetsOnListPagesTests(TestCase):
    """The search behaviour is delivered by two global scripts — if a page stops
    loading them, that page silently loses its search."""

    # Named URLs of the list pages staff actually work from.
    LIST_PAGES = [
        'inventory:dashboard',
        'inventory:transfer_list',
        'inventory:adjustments',
        'inventory:expiry_alerts',
        'inventory:receive_stock',
        'products:product_list',
        'customers:list',
        'sales:list',
    ]

    def setUp(self):
        self.loc = Location.objects.create(name="Asset Shop", address="a")
        self.owner = User.objects.create_user(username="assetowner", password="pw", role="OWNER",
                                              assigned_location=self.loc)
        product = Product.objects.create(name="Thing", slug="thing", sku="THG-1", location=self.loc,
                                         cost_price=Decimal('1'), selling_price=Decimal('2'))
        StockBatch.objects.create(product=product, location=self.loc, quantity=5, cost_price=Decimal('1'))
        Customer.objects.create(first_name="Ama", phone_number="0244000111", location=self.loc)
        self.client.force_login(self.owner)

    def test_every_list_page_loads_the_search_scripts(self):
        for name in self.LIST_PAGES:
            with self.subTest(page=name):
                res = self.client.get(reverse(name))
                self.assertEqual(res.status_code, 200)
                html = res.content.decode()
                self.assertIn('table-search.js', html)
                self.assertIn('searchable-select.js', html)
                self.assertIn('live-search.js', html)

    def test_detail_pages_opt_their_tables_out_of_list_search(self):
        """Receipts and audit tables are not lists — they carry data-no-search."""
        from apps.sales.models import Sale, RegisterSession

        session = RegisterSession.objects.create(
            user=self.owner, location=self.loc, opening_balance=Decimal('0'),
            status=RegisterSession.Status.OPEN,
        )
        sale = Sale.objects.create(location=self.loc, cashier=self.owner, register_session=session,
                                   total_amount=Decimal('10'), amount_paid=Decimal('10'),
                                   status=Sale.Status.COMPLETED)
        html = self.client.get(reverse('sales:detail', args=[sale.pk])).content.decode()
        self.assertIn('data-no-search', html)


class ForgivingSearchTests(TestCase):
    """apps.core.search: word-by-word, any-order, typo-tolerant matching."""

    def setUp(self):
        from apps.core.search import search_queryset
        self.search = search_queryset
        self.loc = Location.objects.create(name="Search Shop", address="a")
        for i, name in enumerate(["Filter - Oil 5W30", "Air Filter K2", "Brake Pad Set", "Engine Oil 1L"]):
            Product.objects.create(name=name, slug=f"p{i}", sku=f"SKU-{i}", location=self.loc,
                                   cost_price=Decimal('1'), selling_price=Decimal('2'))
        Customer.objects.create(first_name="Ama", last_name="Mensah", phone_number="0244000111",
                                location=self.loc)
        Customer.objects.create(first_name="Kofi", last_name="Boateng", phone_number="0201234567",
                                location=self.loc)

    def names(self, query):
        qs = self.search(Product.objects.order_by('name'), query, ['name', 'sku', 'barcode'])
        return list(qs.values_list('name', flat=True))

    def test_words_match_in_any_order(self):
        self.assertEqual(self.names("oil filter"), ["Filter - Oil 5W30"])

    def test_partial_words_match(self):
        self.assertEqual(self.names("filt"), ["Air Filter K2", "Filter - Oil 5W30"])

    def test_words_can_come_from_different_fields(self):
        self.assertEqual(self.names("brake sku-2"), ["Brake Pad Set"])

    def test_typos_are_tolerated_when_nothing_matches_exactly(self):
        self.assertEqual(self.names("brak pda"), ["Brake Pad Set"])
        self.assertEqual(self.names("enigne"), ["Engine Oil 1L"])

    def test_unrelated_query_finds_nothing(self):
        self.assertEqual(self.names("windscreen"), [])

    def test_blank_query_returns_everything(self):
        self.assertEqual(len(self.names("  ")), 4)

    def test_full_name_across_first_and_last_name(self):
        qs = self.search(Customer.objects.all(), "ama mensah", ['first_name', 'last_name', 'phone_number'])
        self.assertEqual([c.first_name for c in qs], ["Ama"])
        qs = self.search(Customer.objects.all(), "mensha", ['first_name', 'last_name', 'phone_number'])
        self.assertEqual([c.first_name for c in qs], ["Ama"])

    def test_list_pages_use_it(self):
        owner = User.objects.create_user(username="searchowner", password="pw", role="OWNER",
                                         assigned_location=self.loc)
        self.client.force_login(owner)
        html = self.client.get(reverse('products:product_list'), {'q': 'oil filter'}).content.decode()
        self.assertIn("Filter - Oil 5W30", html)
        self.assertNotIn("Brake Pad Set", html)
        html = self.client.get(reverse('customers:list'), {'q': 'kofi boateng'}).content.decode()
        self.assertIn("0201234567", html)
        self.assertNotIn("0244000111", html)
