"""
Distributors: their own pages, and selling to them at the distributor price list
straight from the detail page.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.customers.models import Customer
from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.sales.models import Sale, SaleItem, RegisterSession
from apps.users.models import User


class DistributorTestBase(TestCase):
    def setUp(self):
        self.shop = Location.objects.create(name="Dist Shop", address="d")
        self.other_shop = Location.objects.create(name="Dist Other", address="o")

        self.owner = User.objects.create_user(username="distowner", password="pw", role="OWNER",
                                              assigned_location=self.shop)
        self.manager = User.objects.create_user(username="distmgr", password="pw", role="MANAGER",
                                                assigned_location=self.shop)
        self.cashier = User.objects.create_user(username="distcash", password="pw", role="CASHIER",
                                                assigned_location=self.shop)

        # 100 / 90 / 80
        self.product = Product.objects.create(
            name="Barrel Oil", slug="barrel", sku="BO-1", location=self.shop,
            cost_price=Decimal('60.00'), selling_price=Decimal('100.00'),
            wholesale_price=Decimal('90.00'), distributor_price=Decimal('80.00'),
        )
        # No distributor price -> must not be sellable from the distributor page
        self.retail_only = Product.objects.create(
            name="Sponge", slug="sponge", sku="SP-1", location=self.shop,
            cost_price=Decimal('1.00'), selling_price=Decimal('3.00'),
        )
        self.batch = StockBatch.objects.create(product=self.product, location=self.shop,
                                               quantity=50, cost_price=Decimal('60.00'))
        StockBatch.objects.create(product=self.retail_only, location=self.shop,
                                  quantity=20, cost_price=Decimal('1.00'))

        self.distributor = Customer.objects.create(
            company_name="Kumasi Auto Parts", contact_person="Yaw",
            phone_number="0244111222", backup_phone="0209999888",
            email="yaw@kumasiauto.test", address="Adum, Kumasi",
            location=self.shop, is_distributor=True,
        )
        self.plain_customer = Customer.objects.create(
            first_name="Ama", phone_number="0555000111", location=self.shop,
        )
        self.client.force_login(self.manager)

    def on_hand(self):
        return sum(b.quantity for b in StockBatch.objects.filter(product=self.product))

    def sell(self, qty=None, method='CREDIT', amount_paid=None, distributor=None, **extra):
        data = {'payment_method': method}
        if qty is not None:
            data[f'qty_{self.product.id}'] = qty
        if amount_paid is not None:
            data['amount_paid'] = amount_paid
        data.update(extra)
        return self.client.post(
            reverse('customers:distributor_sell', args=[(distributor or self.distributor).pk]),
            data, follow=True,
        )


class DistributorDirectoryTests(DistributorTestBase):
    """Listing, adding and editing distributors."""

    def test_list_shows_distributors_only(self):
        res = self.client.get(reverse('customers:distributor_list'))
        names = [r['distributor'].pk for r in res.context['rows']]
        self.assertEqual(names, [self.distributor.pk])
        self.assertNotIn(self.plain_customer.pk, names)

    def test_list_shows_the_contact_details(self):
        html = self.client.get(reverse('customers:distributor_list')).content.decode()
        for value in ("Kumasi Auto Parts", "Yaw", "0244111222", "0209999888",
                      "yaw@kumasiauto.test", "Adum, Kumasi"):
            self.assertIn(value, html)

    def test_search_matches_every_field(self):
        url = reverse('customers:distributor_list')
        for term in ("Kumasi", "Yaw", "0244111222", "0209999888", "kumasiauto", "Adum"):
            res = self.client.get(url, {'q': term})
            self.assertEqual(len(res.context['rows']), 1, term)

    def test_search_with_no_match(self):
        res = self.client.get(reverse('customers:distributor_list'), {'q': 'Tamale'})
        self.assertEqual(res.context['rows'], [])

    def test_add_a_distributor_at_any_time(self):
        res = self.client.post(reverse('customers:distributor_create'), {
            'company_name': "Takoradi Spares", 'contact_person': "Kojo",
            'first_name': "Kojo", 'last_name': "Mensah",
            'phone_number': "0261234567", 'backup_phone': "0501234567",
            'email': "kojo@spares.test", 'address': "Market Circle",
            'notes': "Pays weekly", 'accepts_marketing_sms': 'on',
        })
        created = Customer.objects.get(company_name="Takoradi Spares")
        self.assertTrue(created.is_distributor)
        self.assertEqual(created.location, self.shop)
        self.assertEqual(created.backup_phone, "0501234567")
        self.assertRedirects(res, reverse('customers:distributor_detail', args=[created.pk]))

    def test_edit_a_distributor(self):
        self.client.post(reverse('customers:distributor_edit', args=[self.distributor.pk]), {
            'company_name': "Kumasi Auto Parts Ltd", 'contact_person': "Yaw Boateng",
            'first_name': "Yaw", 'last_name': "Boateng",
            'phone_number': "0244111222", 'backup_phone': "0275555555",
            'email': "new@kumasiauto.test", 'address': "Suame, Kumasi", 'notes': "",
        })
        self.distributor.refresh_from_db()
        self.assertEqual(self.distributor.company_name, "Kumasi Auto Parts Ltd")
        self.assertEqual(self.distributor.backup_phone, "0275555555")
        self.assertTrue(self.distributor.is_distributor)   # still a distributor

    def test_duplicate_phone_in_the_same_shop_is_refused(self):
        res = self.client.post(reverse('customers:distributor_create'), {
            'company_name': "Copycat", 'phone_number': "0244111222",
        })
        self.assertEqual(res.status_code, 200)
        self.assertFormError(res.context['form'], 'phone_number',
                             "That phone number is already on file for this shop.")
        self.assertFalse(Customer.objects.filter(company_name="Copycat").exists())

    def test_company_name_and_phone_are_required(self):
        res = self.client.post(reverse('customers:distributor_create'), {})
        self.assertEqual(res.status_code, 200)
        self.assertFormError(res.context['form'], 'company_name', 'This field is required.')
        self.assertFormError(res.context['form'], 'phone_number', 'This field is required.')

    def test_display_name_prefers_the_company(self):
        self.assertEqual(self.distributor.get_display_name, "Kumasi Auto Parts")

    def test_a_shop_cannot_see_another_shops_distributors(self):
        foreign = Customer.objects.create(company_name="Accra Motors", phone_number="0300000000",
                                          location=self.other_shop, is_distributor=True)
        res = self.client.get(reverse('customers:distributor_list'))
        self.assertNotIn(foreign.pk, [r['distributor'].pk for r in res.context['rows']])
        self.assertEqual(self.client.get(
            reverse('customers:distributor_detail', args=[foreign.pk])).status_code, 404)

    def test_owner_sees_every_shops_distributors(self):
        Customer.objects.create(company_name="Accra Motors", phone_number="0300000000",
                                location=self.other_shop, is_distributor=True)
        self.client.force_login(self.owner)
        res = self.client.get(reverse('customers:distributor_list'))
        self.assertEqual(len(res.context['rows']), 2)

    def test_cashiers_cannot_reach_the_distributor_pages(self):
        self.client.force_login(self.cashier)
        for url in (reverse('customers:distributor_list'),
                    reverse('customers:distributor_create'),
                    reverse('customers:distributor_detail', args=[self.distributor.pk])):
            self.assertNotEqual(self.client.get(url).status_code, 200, url)


class DistributorDetailPageTests(DistributorTestBase):

    def test_detail_lists_only_products_with_a_distributor_price(self):
        res = self.client.get(reverse('customers:distributor_detail', args=[self.distributor.pk]))
        self.assertEqual([p.pk for p in res.context['sellable']], [self.product.pk])
        self.assertEqual(res.context['unpriced_count'], 1)

    def test_detail_shows_stock_on_hand_per_product(self):
        res = self.client.get(reverse('customers:distributor_detail', args=[self.distributor.pk]))
        self.assertEqual(res.context['sellable'][0].on_hand, 50)

    def test_detail_shows_the_distributor_price_not_retail(self):
        html = self.client.get(
            reverse('customers:distributor_detail', args=[self.distributor.pk])).content.decode()
        self.assertIn('data-unit-price="80.00"', html)

    def test_detail_warns_when_no_product_is_priced_for_distributors(self):
        self.product.distributor_price = None
        self.product.save(update_fields=['distributor_price'])
        html = self.client.get(
            reverse('customers:distributor_detail', args=[self.distributor.pk])).content.decode()
        self.assertIn('has a distributor price set yet', html)

    def test_detail_warns_when_no_register_is_open(self):
        html = self.client.get(
            reverse('customers:distributor_detail', args=[self.distributor.pk])).content.decode()
        self.assertIn('no register open', html)

    def test_detail_shows_money_summary_and_history(self):
        self.sell(qty=2, method='CREDIT')
        res = self.client.get(reverse('customers:distributor_detail', args=[self.distributor.pk]))
        self.assertEqual(res.context['billed'], Decimal('160.00'))
        self.assertEqual(res.context['collected'], Decimal('0.00'))
        self.assertEqual(res.context['owing'], Decimal('160.00'))
        self.assertEqual(res.context['purchase_count'], 1)
        self.assertEqual(len(res.context['purchases']), 1)


class SellToDistributorTests(DistributorTestBase):
    """The money path: distributor prices, stock deduction, payment or debt."""

    def test_sale_uses_the_distributor_price(self):
        self.sell(qty=3, method='CREDIT')
        sale = Sale.objects.get()
        self.assertEqual(sale.total_amount, Decimal('240.00'))   # 3 x 80, not 3 x 100
        item = sale.items.get()
        self.assertEqual(item.unit_price, Decimal('80.00'))
        self.assertEqual(item.price_tier, 'DISTRIBUTOR')

    def test_sale_deducts_stock(self):
        self.assertEqual(self.on_hand(), 50)
        self.sell(qty=6, method='CREDIT')
        self.assertEqual(self.on_hand(), 44)

    def test_sale_is_attached_to_the_distributor(self):
        self.sell(qty=1, method='CREDIT')
        sale = Sale.objects.get()
        self.assertEqual(sale.customer, self.distributor)
        self.assertEqual(sale.location, self.shop)
        self.assertEqual(sale.cashier, self.manager)

    def test_credit_sale_leaves_the_whole_amount_owing(self):
        self.sell(qty=2, method='CREDIT')
        sale = Sale.objects.get()
        self.assertEqual(sale.amount_paid, Decimal('0.00'))
        self.assertEqual(sale.balance_remaining, Decimal('160.00'))

    def test_part_payment_records_the_balance_as_debt(self):
        self.sell(qty=10, method='MOMO', amount_paid='500.00')   # 800 due
        sale = Sale.objects.get()
        self.assertEqual(sale.total_amount, Decimal('800.00'))
        self.assertEqual(sale.amount_paid, Decimal('500.00'))
        self.assertEqual(sale.balance_remaining, Decimal('300.00'))

    def test_full_payment_settles_the_sale(self):
        self.sell(qty=2, method='MOMO', amount_paid='160.00')
        sale = Sale.objects.get()
        self.assertEqual(sale.balance_remaining, Decimal('0.00'))
        self.assertEqual(sale.payments.count(), 1)

    def test_cash_without_an_open_register_is_refused(self):
        res = self.sell(qty=1, method='CASH', amount_paid='80.00')
        self.assertContains(res, 'Cash cannot be accepted without an open register')
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(self.on_hand(), 50)

    def test_cash_with_an_open_register_lands_in_the_drawer(self):
        session = RegisterSession.objects.create(
            user=self.manager, location=self.shop, opening_balance=Decimal('0.00'),
            status=RegisterSession.Status.OPEN,
        )
        self.sell(qty=2, method='CASH', amount_paid='160.00')
        session.refresh_from_db()
        self.assertEqual(session.total_cash_sales, Decimal('160.00'))
        self.assertEqual(Sale.objects.get().register_session, session)

    def test_multiple_products_in_one_order(self):
        second = Product.objects.create(
            name="Grease Tub", slug="grease", sku="GR-1", location=self.shop,
            cost_price=Decimal('5.00'), selling_price=Decimal('20.00'),
            distributor_price=Decimal('15.00'),
        )
        StockBatch.objects.create(product=second, location=self.shop, quantity=10,
                                  cost_price=Decimal('5.00'))
        self.client.post(reverse('customers:distributor_sell', args=[self.distributor.pk]), {
            f'qty_{self.product.id}': 2, f'qty_{second.id}': 3, 'payment_method': 'CREDIT',
        }, follow=True)
        sale = Sale.objects.get()
        self.assertEqual(sale.total_amount, Decimal('205.00'))   # 160 + 45
        self.assertEqual(sale.items.count(), 2)
        self.assertEqual(StockBatch.objects.get(product=second).quantity, 7)

    def test_product_without_a_distributor_price_cannot_be_forced_through(self):
        res = self.client.post(
            reverse('customers:distributor_sell', args=[self.distributor.pk]),
            {f'qty_{self.retail_only.id}': 1, 'payment_method': 'CREDIT'}, follow=True)
        self.assertContains(res, 'no distributor price set')
        self.assertFalse(Sale.objects.exists())

    def test_overselling_is_refused_and_stock_untouched(self):
        res = self.sell(qty=999, method='CREDIT')
        self.assertContains(res, 'Insufficient stock')
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(self.on_hand(), 50)

    def test_empty_order_is_rejected(self):
        res = self.sell(qty=0, method='CREDIT')
        self.assertContains(res, 'Enter a quantity for at least one product')
        self.assertFalse(Sale.objects.exists())

    def test_non_numeric_quantity_is_rejected(self):
        res = self.sell(qty='abc', method='CREDIT')
        self.assertContains(res, 'whole numbers')
        self.assertFalse(Sale.objects.exists())

    def test_bad_amount_paid_is_rejected(self):
        res = self.sell(qty=1, method='MOMO', amount_paid='lots')
        self.assertContains(res, 'must be a number')
        self.assertFalse(Sale.objects.exists())

    def test_nothing_is_written_when_one_line_fails(self):
        res = self.client.post(
            reverse('customers:distributor_sell', args=[self.distributor.pk]),
            {f'qty_{self.product.id}': 1, f'qty_{self.retail_only.id}': 1,
             'payment_method': 'CREDIT'}, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleItem.objects.exists())
        self.assertEqual(self.on_hand(), 50)

    def test_distributor_stats_are_updated(self):
        self.sell(qty=2, method='MOMO', amount_paid='160.00')
        self.distributor.refresh_from_db()
        self.assertEqual(self.distributor.total_spent, Decimal('160.00'))
        self.assertEqual(self.distributor.total_visits, 1)
        self.assertIsNotNone(self.distributor.last_visit_date)

    def test_the_sale_appears_in_normal_sales_history(self):
        self.sell(qty=1, method='CREDIT')
        sale = Sale.objects.get()
        res = self.client.get(reverse('sales:list'), {'date': ''})
        self.assertIn(sale.pk, [s.pk for s in res.context['sales']])

    def test_the_debt_appears_in_arrears(self):
        self.sell(qty=2, method='CREDIT')
        res = self.client.get(reverse('sales:list'), {'date': '', 'status': 'DEBT'})
        self.assertEqual([s.total_amount for s in res.context['sales']], [Decimal('160.00')])

    def test_receipt_shows_the_distributor_tier(self):
        self.sell(qty=1, method='CREDIT')
        sale = Sale.objects.get()
        html = self.client.get(reverse('sales:detail', args=[sale.pk])).content.decode()
        self.assertIn('Distributor', html)

    def test_cannot_sell_to_a_plain_customer_through_this_route(self):
        res = self.client.post(
            reverse('customers:distributor_sell', args=[self.plain_customer.pk]),
            {f'qty_{self.product.id}': 1, 'payment_method': 'CREDIT'})
        self.assertEqual(res.status_code, 404)
        self.assertFalse(Sale.objects.exists())

    def test_cannot_sell_to_another_shops_distributor(self):
        foreign = Customer.objects.create(company_name="Accra Motors", phone_number="0300000000",
                                          location=self.other_shop, is_distributor=True)
        res = self.client.post(
            reverse('customers:distributor_sell', args=[foreign.pk]),
            {f'qty_{self.product.id}': 1, 'payment_method': 'CREDIT'})
        self.assertEqual(res.status_code, 404)
        self.assertFalse(Sale.objects.exists())

    def test_cashier_cannot_sell_from_the_distributor_page(self):
        self.client.force_login(self.cashier)
        self.client.post(reverse('customers:distributor_sell', args=[self.distributor.pk]),
                         {f'qty_{self.product.id}': 1, 'payment_method': 'CREDIT'})
        self.assertFalse(Sale.objects.exists())

    def test_stock_comes_off_fefo_across_batches(self):
        from datetime import timedelta
        from django.utils import timezone
        soon = timezone.localdate() + timedelta(days=5)
        StockBatch.objects.filter(pk=self.batch.pk).update(quantity=3, expiry_date=soon)
        later = StockBatch.objects.create(
            product=self.product, location=self.shop, quantity=10,
            cost_price=Decimal('65.00'), expiry_date=timezone.localdate() + timedelta(days=200),
        )
        self.sell(qty=5, method='CREDIT')
        self.batch.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(self.batch.quantity, 0)    # soonest expiry drained first
        self.assertEqual(later.quantity, 8)

    def test_redirects_to_the_receipt(self):
        res = self.client.post(
            reverse('customers:distributor_sell', args=[self.distributor.pk]),
            {f'qty_{self.product.id}': 1, 'payment_method': 'CREDIT'})
        self.assertRedirects(res, reverse('sales:detail', args=[Sale.objects.get().pk]))


class DistributorSidebarTests(DistributorTestBase):
    """The menu entry must exist, and only for the roles allowed to use it."""

    def test_link_shows_for_owner_and_manager(self):
        for user in (self.owner, self.manager):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                html = self.client.get(reverse('customers:list')).content.decode()
                self.assertIn(reverse('customers:distributor_list'), html)
                self.assertIn('Distributors', html)

    def test_link_hidden_from_roles_that_cannot_use_it(self):
        for role in ('CASHIER', 'SALESPERSON', 'WAREHOUSE_STAFF', 'ACCOUNTANT'):
            with self.subTest(role=role):
                user = User.objects.create_user(
                    username=f'sidebar{role}', password='pw', role=role,
                    assigned_location=self.shop,
                )
                self.client.force_login(user)
                res = self.client.get(reverse('sales:pos'))
                if res.status_code == 200:
                    self.assertNotIn(reverse('customers:distributor_list'),
                                     res.content.decode())
