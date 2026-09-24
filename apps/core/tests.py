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
