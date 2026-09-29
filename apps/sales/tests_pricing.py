"""
Price tiers: Normal (retail), Wholesale and Distributor.

The money rule this file defends: the browser chooses a PRICE LIST, never an
amount. The server looks every figure up in the catalogue, so a stale, edited or
hostile page cannot change what gets banked.
"""
import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.sales.models import Sale, SaleItem, RegisterSession, SalePayment
from apps.users.models import User

RETAIL, WHOLESALE, DISTRIBUTOR = 'RETAIL', 'WHOLESALE', 'DISTRIBUTOR'


class PricingTestBase(TestCase):
    def setUp(self):
        self.loc = Location.objects.create(name="Tier Shop", address="t")
        self.other_loc = Location.objects.create(name="Other Tier Shop", address="o")
        self.cashier = User.objects.create_user(username="tiercash", password="pw", role="CASHIER",
                                                assigned_location=self.loc)

        # Fully priced: 100 / 90 / 80
        self.product = Product.objects.create(
            name="Tyre 195/65", slug="tyre", sku="TY-1", location=self.loc,
            cost_price=Decimal('60.00'), selling_price=Decimal('100.00'),
            wholesale_price=Decimal('90.00'), distributor_price=Decimal('80.00'),
        )
        # Retail only
        self.retail_only = Product.objects.create(
            name="Air Freshener", slug="air", sku="AF-1", location=self.loc,
            cost_price=Decimal('2.00'), selling_price=Decimal('5.00'),
        )
        # Retail + wholesale, no distributor
        self.no_distributor = Product.objects.create(
            name="Wiper Fluid", slug="wiper", sku="WF-1", location=self.loc,
            cost_price=Decimal('4.00'), selling_price=Decimal('12.00'),
            wholesale_price=Decimal('10.00'),
        )

        for product in (self.product, self.retail_only, self.no_distributor):
            StockBatch.objects.create(product=product, location=self.loc, quantity=100,
                                      cost_price=product.cost_price)

        self.session = RegisterSession.objects.create(
            user=self.cashier, location=self.loc, opening_balance=Decimal('0.00'),
            status=RegisterSession.Status.OPEN,
        )
        self.client.force_login(self.cashier)

    def sell(self, lines, payments=None, customer_id=None):
        """lines: [{'id':.., 'qty':.., 'tier':.., 'price':..(optional)}]"""
        if payments is None:
            payments = [{'method': 'CASH', 'amount': '100000.00'}]
        return self.client.post(
            reverse('sales:process_sale'),
            data=json.dumps({'cart': lines, 'payments': payments, 'customer_id': customer_id}),
            content_type='application/json',
        )

    def line(self, product=None, qty=1, tier=RETAIL, price=None):
        entry = {'id': (product or self.product).id, 'qty': qty, 'tier': tier}
        if price is not None:
            entry['price'] = price
        return entry


class PriceTierResolutionTests(PricingTestBase):
    """Each tier charges its own price, taken from the catalogue."""

    def test_retail_tier_charges_the_selling_price(self):
        body = self.sell([self.line(tier=RETAIL, qty=2)]).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.total_amount, Decimal('200.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('100.00'))

    def test_wholesale_tier_charges_the_wholesale_price(self):
        body = self.sell([self.line(tier=WHOLESALE, qty=2)]).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.total_amount, Decimal('180.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('90.00'))

    def test_distributor_tier_charges_the_distributor_price(self):
        body = self.sell([self.line(tier=DISTRIBUTOR, qty=2)]).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.total_amount, Decimal('160.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('80.00'))

    def test_missing_tier_defaults_to_retail(self):
        body = self.sell([{'id': self.product.id, 'qty': 1}]).json()
        self.assertTrue(body['success'], body)
        item = SaleItem.objects.get()
        self.assertEqual(item.unit_price, Decimal('100.00'))
        self.assertEqual(item.price_tier, RETAIL)

    def test_tier_is_case_insensitive(self):
        body = self.sell([self.line(tier='wholesale')]).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(SaleItem.objects.get().unit_price, Decimal('90.00'))

    def test_the_tier_is_recorded_on_every_line(self):
        self.sell([self.line(tier=DISTRIBUTOR)])
        self.assertEqual(SaleItem.objects.get().price_tier, DISTRIBUTOR)

    def test_mixed_tiers_in_one_sale(self):
        body = self.sell([
            self.line(tier=RETAIL, qty=1),                              # 100
            self.line(product=self.no_distributor, tier=WHOLESALE, qty=2),  # 20
        ]).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.total_amount, Decimal('120.00'))
        self.assertEqual(
            {(i.product.sku, i.price_tier, i.unit_price) for i in sale.items.all()},
            {('TY-1', RETAIL, Decimal('100.00')), ('WF-1', WHOLESALE, Decimal('10.00'))},
        )

    def test_same_product_twice_at_two_different_tiers(self):
        body = self.sell([
            self.line(tier=RETAIL, qty=1),
            self.line(tier=DISTRIBUTOR, qty=1),
        ]).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.total_amount, Decimal('180.00'))  # 100 + 80
        self.assertEqual(sorted(i.unit_price for i in sale.items.all()),
                         [Decimal('80.00'), Decimal('100.00')])


class PriceTierStrictnessTests(PricingTestBase):
    """The server refuses anything it cannot justify from the catalogue."""

    def test_unknown_tier_is_rejected(self):
        res = self.sell([self.line(tier='VIP')])
        self.assertEqual(res.status_code, 400)
        self.assertIn('Unknown price type', res.json()['message'])
        self.assertFalse(Sale.objects.exists())

    def test_tier_with_no_price_set_is_rejected_not_silently_downgraded(self):
        res = self.sell([self.line(product=self.retail_only, tier=WHOLESALE)])
        self.assertEqual(res.status_code, 400)
        self.assertIn('no wholesale price set', res.json()['message'])
        self.assertFalse(Sale.objects.exists())

    def test_distributor_tier_rejected_when_only_wholesale_exists(self):
        res = self.sell([self.line(product=self.no_distributor, tier=DISTRIBUTOR)])
        self.assertEqual(res.status_code, 400)
        self.assertIn('no distributor price set', res.json()['message'])

    def test_a_tampered_price_is_rejected(self):
        """The classic attack: ask for the distributor tier but claim a price of 1."""
        res = self.sell([self.line(tier=DISTRIBUTOR, price='1.00')])
        self.assertEqual(res.status_code, 400)
        self.assertIn('out of date', res.json()['message'])
        self.assertFalse(Sale.objects.exists())

    def test_a_matching_claimed_price_is_accepted(self):
        body = self.sell([self.line(tier=WHOLESALE, price='90.00')]).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(Sale.objects.get().total_amount, Decimal('90.00'))

    def test_claimed_price_from_a_stale_page_is_rejected(self):
        """Prices changed after the cashier loaded the page."""
        self.product.wholesale_price = Decimal('95.00')
        self.product.save(update_fields=['wholesale_price'])
        res = self.sell([self.line(tier=WHOLESALE, price='90.00')])  # old price
        self.assertEqual(res.status_code, 400)
        self.assertIn('out of date', res.json()['message'])

    def test_price_is_taken_from_the_catalogue_not_the_payload(self):
        """No claimed price at all -> the server still charges the right money."""
        body = self.sell([self.line(tier=WHOLESALE, qty=3)]).json()
        self.assertEqual(Sale.objects.get(id=body['sale_id']).total_amount, Decimal('270.00'))

    def test_junk_claimed_price_is_rejected(self):
        res = self.sell([self.line(tier=RETAIL, price='not-a-number')])
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_blank_claimed_price_is_ignored(self):
        body = self.sell([self.line(tier=RETAIL, price='')]).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(Sale.objects.get().total_amount, Decimal('100.00'))

    def test_a_zero_priced_tier_is_refused(self):
        self.product.distributor_price = Decimal('0.00')
        self.product.save(update_fields=['distributor_price'])
        res = self.sell([self.line(tier=DISTRIBUTOR)])
        self.assertEqual(res.status_code, 400)
        self.assertIn('invalid price', res.json()['message'])

    def test_cannot_sell_another_shops_product(self):
        foreign = Product.objects.create(name="Foreign", slug="foreign", sku="FG-1",
                                         location=self.other_loc, cost_price=Decimal('1'),
                                         selling_price=Decimal('9'))
        StockBatch.objects.create(product=foreign, location=self.other_loc, quantity=10,
                                  cost_price=Decimal('1'))
        res = self.sell([{'id': foreign.id, 'qty': 1, 'tier': RETAIL}])
        self.assertEqual(res.status_code, 400)
        self.assertIn('not found', res.json()['message'])

    def test_nothing_is_written_when_one_line_is_invalid(self):
        res = self.sell([
            self.line(tier=RETAIL, qty=1),
            self.line(product=self.retail_only, tier=DISTRIBUTOR, qty=1),  # invalid
        ])
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleItem.objects.exists())
        for batch in StockBatch.objects.all():
            self.assertEqual(batch.quantity, 100)  # no stock moved


class PriceTierMoneyFlowTests(PricingTestBase):
    """Payments, change, debt and the drawer must all follow the tier price."""

    def test_change_is_calculated_against_the_tier_price(self):
        body = self.sell([self.line(tier=DISTRIBUTOR, qty=1)],
                         payments=[{'method': 'CASH', 'amount': '100.00'}]).json()
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.total_amount, Decimal('80.00'))
        self.assertEqual(sale.amount_paid, Decimal('80.00'))
        self.assertEqual(sale.change_due, Decimal('20.00'))
        self.assertEqual(body['change_due'], 20.0)

    def test_underpayment_at_a_tier_price_becomes_debt(self):
        from apps.customers.models import Customer
        customer = Customer.objects.create(first_name="Ama", phone_number="0244000111",
                                           location=self.loc)
        body = self.sell([self.line(tier=WHOLESALE, qty=10)],   # 900
                         payments=[{'method': 'CASH', 'amount': '500.00'}],
                         customer_id=customer.id).json()
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.total_amount, Decimal('900.00'))
        self.assertEqual(sale.amount_paid, Decimal('500.00'))
        self.assertEqual(sale.balance_remaining, Decimal('400.00'))
        self.assertEqual(body['balance_due'], 400.0)

    def test_payments_reconcile_to_the_tier_total(self):
        body = self.sell([self.line(tier=WHOLESALE, qty=2)],
                         payments=[{'method': 'CASH', 'amount': '180.00'}]).json()
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sum(p.amount for p in sale.payments.all()), Decimal('180.00'))

    def test_drawer_records_the_tier_price_not_the_retail_price(self):
        self.sell([self.line(tier=DISTRIBUTOR, qty=1)],
                  payments=[{'method': 'CASH', 'amount': '80.00'}])
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, Decimal('80.00'))

    def test_split_payment_across_a_wholesale_total(self):
        body = self.sell([self.line(tier=WHOLESALE, qty=2)],   # 180
                         payments=[{'method': 'CASH', 'amount': '100.00'},
                                   {'method': 'MOMO', 'amount': '80.00'}]).json()
        sale = Sale.objects.get(id=body['sale_id'])
        self.assertEqual(sale.amount_paid, Decimal('180.00'))
        self.assertEqual(sale.balance_remaining, Decimal('0.00'))
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, Decimal('100.00'))
        self.assertEqual(self.session.total_momo_sales, Decimal('80.00'))

    def test_margin_is_measured_against_the_tier_price(self):
        self.sell([self.line(tier=DISTRIBUTOR, qty=1)])
        item = SaleItem.objects.get()
        self.assertEqual(item.unit_price, Decimal('80.00'))
        self.assertEqual(item.unit_cost, Decimal('60.00'))   # from the batch
        self.assertEqual(item.unit_price - item.unit_cost, Decimal('20.00'))

    def test_stock_moves_the_same_whatever_the_tier(self):
        self.sell([self.line(tier=DISTRIBUTOR, qty=4)])
        batch = StockBatch.objects.get(product=self.product)
        self.assertEqual(batch.quantity, 96)

    def test_refunding_a_wholesale_line_returns_the_wholesale_money(self):
        body = self.sell([self.line(tier=WHOLESALE, qty=2)],
                         payments=[{'method': 'CASH', 'amount': '180.00'}]).json()
        sale = Sale.objects.get(id=body['sale_id'])
        item = sale.items.get()

        manager = User.objects.create_user(username="tiermgr", password="pw", role="MANAGER",
                                           assigned_location=self.loc)
        RegisterSession.objects.create(user=manager, location=self.loc,
                                       opening_balance=Decimal('0.00'),
                                       status=RegisterSession.Status.OPEN)
        self.client.force_login(manager)
        self.client.post(reverse('sales:refund', args=[sale.pk]), {
            'refund_items': [item.id], 'reason': 'Customer Return', 'refund_method': 'CASH',
        })

        sale.refresh_from_db()
        # The refund must give back 180 (wholesale), not 200 (retail).
        refund = sale.payments.filter(amount__lt=0).get()
        self.assertEqual(refund.amount, Decimal('-180.00'))
        self.assertEqual(sale.total_amount, Decimal('0.00'))
        self.assertEqual(StockBatch.objects.get(product=self.product).quantity, 100)


class PriceTierReceiptTests(PricingTestBase):
    """Receipts must print the server's figures and name the price list used."""

    def test_response_carries_the_recorded_lines(self):
        body = self.sell([self.line(tier=WHOLESALE, qty=3)]).json()
        line = body['lines'][0]
        self.assertEqual(line['tier'], WHOLESALE)
        self.assertEqual(line['tier_label'], 'Wholesale')
        self.assertEqual(line['unit_price'], 90.0)
        self.assertEqual(line['line_total'], 270.0)
        self.assertEqual(line['qty'], 3)
        self.assertEqual(line['name'], 'Tyre 195/65')

    def test_response_lines_sum_to_the_recorded_total(self):
        body = self.sell([
            self.line(tier=RETAIL, qty=1),
            self.line(product=self.no_distributor, tier=WHOLESALE, qty=2),
        ]).json()
        self.assertEqual(sum(l['line_total'] for l in body['lines']), body['total_amount'])

    def test_stored_receipt_shows_the_tier_badge(self):
        body = self.sell([self.line(tier=DISTRIBUTOR)]).json()
        html = self.client.get(reverse('sales:detail', args=[body['sale_id']])).content.decode()
        self.assertIn('Distributor', html)

    def test_stored_receipt_has_no_badge_for_a_normal_sale(self):
        body = self.sell([self.line(tier=RETAIL)]).json()
        html = self.client.get(reverse('sales:detail', args=[body['sale_id']])).content.decode()
        self.assertNotIn('bg-label-warning">\n                                            Wholesale', html)


class ProductPriceHelperTests(TestCase):
    """The single price lookup every caller must go through."""

    def setUp(self):
        self.loc = Location.objects.create(name="Helper Shop", address="h")
        self.product = Product.objects.create(
            name="Thing", slug="thing-h", sku="TH-9", location=self.loc,
            cost_price=Decimal('1.00'), selling_price=Decimal('10.00'),
            wholesale_price=Decimal('8.00'), distributor_price=Decimal('7.00'),
        )

    def test_price_for_each_tier(self):
        self.assertEqual(self.product.price_for_tier(RETAIL), Decimal('10.00'))
        self.assertEqual(self.product.price_for_tier(WHOLESALE), Decimal('8.00'))
        self.assertEqual(self.product.price_for_tier(DISTRIBUTOR), Decimal('7.00'))

    def test_unset_tier_returns_none_rather_than_falling_back(self):
        self.product.distributor_price = None
        self.assertIsNone(self.product.price_for_tier(DISTRIBUTOR))

    def test_unknown_tier_returns_none(self):
        self.assertIsNone(self.product.price_for_tier('PLATINUM'))

    def test_available_tiers_lists_only_configured_prices(self):
        self.assertEqual([t.value for t in self.product.available_tiers()],
                         [RETAIL, WHOLESALE, DISTRIBUTOR])
        self.product.wholesale_price = None
        self.product.distributor_price = None
        self.assertEqual([t.value for t in self.product.available_tiers()], [RETAIL])


class PosCatalogueTierTests(PricingTestBase):
    """The POS page and search API must expose every tier honestly."""

    def test_search_api_returns_all_three_prices(self):
        res = self.client.get(reverse('sales:product_search'), {'q': 'Tyre'})
        row = res.json()['results'][0]
        self.assertEqual(row['prices'], {'RETAIL': 100.0, 'WHOLESALE': 90.0, 'DISTRIBUTOR': 80.0})

    def test_search_api_reports_unset_tiers_as_null(self):
        res = self.client.get(reverse('sales:product_search'), {'q': 'Air Freshener'})
        row = res.json()['results'][0]
        self.assertEqual(row['prices']['RETAIL'], 5.0)
        self.assertIsNone(row['prices']['WHOLESALE'])
        self.assertIsNone(row['prices']['DISTRIBUTOR'])

    def test_pos_page_renders_every_tier_on_the_card(self):
        html = self.client.get(reverse('sales:pos')).content.decode()
        self.assertIn('data-price-retail="100.00"', html)
        self.assertIn('data-price-wholesale="90.00"', html)
        self.assertIn('data-price-distributor="80.00"', html)

    def test_pos_page_leaves_unset_tiers_blank_on_the_card(self):
        html = self.client.get(reverse('sales:pos')).content.decode()
        self.assertIn('data-price-wholesale=""', html)      # the retail-only product
        self.assertIn('data-price-distributor=""', html)

    def test_pos_page_offers_the_three_way_switch(self):
        html = self.client.get(reverse('sales:pos')).content.decode()
        for tier in (RETAIL, WHOLESALE, DISTRIBUTOR):
            self.assertIn(f'data-tier="{tier}"', html)
        self.assertNotIn('wholesale-mode-check', html)   # the old broken checkbox is gone
