"""
Edge cases for the money paths: quantities, payments, change, credit, drawers,
settlements, refunds, retries — and the reconcile_sales repair command.

Every money scenario ends with `assert_tallies()`, which runs the same checks
as `manage.py reconcile_sales` and fails if any stored total disagrees with
the rows behind it. "The sales tally" is asserted as an invariant, not per field.
"""
import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.sales import services
from apps.sales.models import RegisterSession, Sale, SaleItem, SalePayment
from apps.sales.reconcile import reconcile
from apps.users.models import User

D = Decimal


class MoneyTestBase(TestCase):

    def setUp(self):
        self.loc = Location.objects.create(name="Main Shop", address="1 St")
        self.other_loc = Location.objects.create(name="Far Shop", address="2 St")
        self.cashier = User.objects.create_user(username="cash", password="pw", role="CASHIER",
                                                assigned_location=self.loc)
        self.cashier2 = User.objects.create_user(username="cash2", password="pw", role="CASHIER",
                                                 assigned_location=self.loc)
        self.manager = User.objects.create_user(username="mgr", password="pw", role="MANAGER",
                                                assigned_location=self.loc)
        self.owner = User.objects.create_user(username="own", password="pw", role="OWNER",
                                              assigned_location=self.loc)
        self.far_cashier = User.objects.create_user(username="far", password="pw", role="CASHIER",
                                                    assigned_location=self.other_loc)

        # 10.00 each, stock split over three batches: 3 + 3 + 5 = 11 on hand.
        self.oil = Product.objects.create(name="Engine Oil", sku="OIL", location=self.loc,
                                          cost_price=D("6.00"), selling_price=D("10.00"))
        self.batches = [
            StockBatch.objects.create(product=self.oil, location=self.loc, quantity=q, cost_price=D("6.00"))
            for q in (3, 3, 5)
        ]
        # 33.33 — sums that drift in floating point.
        self.filter = Product.objects.create(name="Oil Filter", sku="FLT", location=self.loc,
                                             cost_price=D("20.00"), selling_price=D("33.33"),
                                             wholesale_price=D("30.00"), tax_rate=D("15.00"))
        StockBatch.objects.create(product=self.filter, location=self.loc, quantity=100, cost_price=D("20.00"))

        self.far_product = Product.objects.create(name="Far Oil", sku="OIL", location=self.other_loc,
                                                  cost_price=D("1"), selling_price=D("2"))
        StockBatch.objects.create(product=self.far_product, location=self.other_loc, quantity=50,
                                  cost_price=D("1"))

        self.customer = Customer.objects.create(phone_number="0240000001", first_name="Ama", location=self.loc)
        self.far_customer = Customer.objects.create(phone_number="0240000002", first_name="Kofi",
                                                    location=self.other_loc)

    # ---- helpers ------------------------------------------------------------
    def open_session(self, user, opening="100.00"):
        return RegisterSession.objects.create(user=user, location=user.assigned_location,
                                              opening_balance=D(opening), status=RegisterSession.Status.OPEN)

    def post_sale(self, cart, payments, customer_id=None, client_ref=None, raw=None):
        body = raw if raw is not None else json.dumps(
            {'cart': cart, 'payments': payments, 'customer_id': customer_id, 'client_ref': client_ref})
        return self.client.post(reverse('sales:process_sale'), data=body, content_type='application/json')

    def sell(self, qty=1, product=None, payments=None, **kw):
        product = product or self.oil
        if payments is None:
            payments = [{'method': 'CASH', 'amount': str(product.selling_price * qty)}]
        return self.post_sale([{'id': product.id, 'qty': qty}], payments, **kw)

    def stock(self, product=None):
        return sum(b.quantity for b in StockBatch.objects.filter(product=product or self.oil))

    def units_sold(self, sale):
        return sum(i.quantity for i in sale.items.all())

    def assert_tallies(self):
        report = reconcile(fix=False)
        # possible-duplicate is a hint for a person, not a tally failure.
        problems = [f"{f.check}: {f.subject}: {f.detail}" for f in report.findings
                    if f.check != 'possible-duplicate']
        self.assertEqual(problems, [], "records do not tally")

    def assert_rejected(self, resp, fragment=None):
        body = resp.json()
        self.assertEqual(resp.status_code, 400, body)
        self.assertFalse(body['success'])
        if fragment:
            self.assertIn(fragment.lower(), body['message'].lower())
        return body


# =============================================================================
# Quantities: "sold 8, it recorded 11"
# =============================================================================
class QuantityTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)
        self.session = self.open_session(self.cashier)

    def test_selling_8_records_exactly_8_across_batches(self):
        body = self.sell(8).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(pk=body['sale_id'])
        self.assertEqual(self.units_sold(sale), 8)
        self.assertEqual(self.stock(), 3)                       # 11 - 8
        self.assertEqual(sale.items.count(), 3)                 # 3 + 3 + 2 FEFO
        self.assertEqual(sale.total_amount, D("80.00"))
        self.assertEqual([l['qty'] for l in body['lines']], [8])  # receipt shows one line of 8
        self.assert_tallies()

    def test_same_product_on_two_lines_adds_up(self):
        body = self.post_sale([{'id': self.oil.id, 'qty': 5}, {'id': self.oil.id, 'qty': 3}],
                              [{'method': 'CASH', 'amount': '80'}]).json()
        sale = Sale.objects.get(pk=body['sale_id'])
        self.assertEqual(self.units_sold(sale), 8)
        self.assertEqual(self.stock(), 3)
        self.assert_tallies()

    def test_same_product_two_price_lists(self):
        body = self.post_sale(
            [{'id': self.filter.id, 'qty': 2, 'tier': 'RETAIL'},
             {'id': self.filter.id, 'qty': 3, 'tier': 'WHOLESALE'}],
            [{'method': 'CASH', 'amount': '156.66'}]).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(pk=body['sale_id'])
        self.assertEqual(sale.total_amount, D("156.66"))        # 66.66 + 90.00
        self.assertEqual(self.stock(self.filter), 95)
        self.assert_tallies()

    def test_selling_the_last_unit_empties_stock_exactly(self):
        self.assertTrue(self.sell(11).json()['success'])
        self.assertEqual(self.stock(), 0)
        self.assertFalse(StockBatch.objects.filter(quantity__lt=0).exists())

    def test_one_more_than_stock_is_refused_and_nothing_moves(self):
        self.assert_rejected(self.sell(12), 'insufficient stock')
        self.assertEqual(self.stock(), 11)
        self.assertFalse(Sale.objects.exists())
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, D("0.00"))

    def test_a_failing_second_line_rolls_back_the_first(self):
        resp = self.post_sale([{'id': self.filter.id, 'qty': 2}, {'id': self.oil.id, 'qty': 50}],
                              [{'method': 'CASH', 'amount': '1000'}])
        self.assert_rejected(resp, 'insufficient stock')
        self.assertEqual(self.stock(self.filter), 100)
        self.assertEqual(self.stock(), 11)
        self.assertFalse(SalePayment.objects.exists())

    def test_quantity_given_as_text_is_accepted(self):
        self.assertTrue(self.post_sale([{'id': self.oil.id, 'qty': "8"}],
                                       [{'method': 'CASH', 'amount': '80'}]).json()['success'])
        self.assertEqual(self.stock(), 3)

    def test_quantity_given_as_whole_float_is_accepted(self):
        self.assertTrue(self.post_sale([{'id': self.oil.id, 'qty': 8.0}],
                                       [{'method': 'CASH', 'amount': '80'}]).json()['success'])
        self.assertEqual(self.stock(), 3)

    def test_fractional_quantity_is_refused_not_truncated(self):
        # int(2.5) used to sell 2 silently.
        for qty in (2.5, "2.5", "8.9"):
            self.assert_rejected(self.post_sale([{'id': self.oil.id, 'qty': qty}],
                                                [{'method': 'CASH', 'amount': '100'}]), 'whole')
        self.assertEqual(self.stock(), 11)

    def test_junk_quantities_are_refused(self):
        for qty in (True, None, "abc", "", "1e400", float('inf'), [], {}):
            resp = self.post_sale([{'id': self.oil.id, 'qty': qty}], [{'method': 'CASH', 'amount': '10'}])
            self.assertEqual(resp.status_code, 400, (qty, resp.content))
        self.assertFalse(Sale.objects.exists())

    def test_negative_quantity_cannot_be_used_as_a_refund(self):
        self.assert_rejected(self.post_sale([{'id': self.oil.id, 'qty': -3}],
                                            [{'method': 'CASH', 'amount': '10'}]), 'negative')
        self.assertEqual(self.stock(), 11)

    def test_huge_quantity_is_refused(self):
        self.assert_rejected(self.post_sale([{'id': self.oil.id, 'qty': 10 ** 9}],
                                            [{'method': 'CASH', 'amount': '10'}]), 'too large')

    def test_zero_quantity_lines_are_ignored_but_an_all_zero_cart_is_empty(self):
        body = self.post_sale([{'id': self.oil.id, 'qty': 0}, {'id': self.filter.id, 'qty': 1}],
                              [{'method': 'CASH', 'amount': '33.33'}]).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(self.stock(), 11)
        self.assert_rejected(self.post_sale([{'id': self.oil.id, 'qty': 0}],
                                            [{'method': 'CASH', 'amount': '10'}]), 'empty')

    def test_malformed_carts_are_refused(self):
        for cart in ("nope", [1, 2], [{'qty': 1}], [{'id': 'x', 'qty': 1}], {'id': self.oil.id}):
            resp = self.post_sale(cart, [{'method': 'CASH', 'amount': '10'}])
            self.assertEqual(resp.status_code, 400, (cart, resp.content))
        self.assertEqual(self.post_sale(None, None, raw='[1,2]').status_code, 400)
        self.assertEqual(self.post_sale(None, None, raw='{bad json').status_code, 400)

    def test_another_shops_product_cannot_be_sold_here(self):
        self.assert_rejected(self.sell(1, product=self.far_product), 'not found')
        self.assertEqual(self.stock(self.far_product), 50)

    def test_inactive_product_cannot_be_sold(self):
        self.oil.is_active = False
        self.oil.save()
        self.assert_rejected(self.sell(1), 'not found')

    def test_stock_that_changes_mid_sale_aborts_without_overselling(self):
        # Simulate another till taking the units between our check and our write:
        # the lock handed us batches that claim more stock than the database has.
        real_lock = services._lock_batches

        def stale_lock(qty_per_product, location, resolved):
            batches = real_lock(qty_per_product, location, resolved)
            StockBatch.objects.filter(product=self.oil).update(quantity=0)
            return batches

        with mock.patch.object(services, '_lock_batches', stale_lock):
            self.assert_rejected(self.sell(2), 'changed')
        self.assertFalse(StockBatch.objects.filter(quantity__lt=0).exists())
        self.assertFalse(Sale.objects.exists())


# =============================================================================
# Retries / double submits
# =============================================================================
class IdempotencyTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)
        self.session = self.open_session(self.cashier)

    def test_same_checkout_sent_twice_sells_once(self):
        first = self.sell(8, client_ref="ref-1").json()
        again = self.sell(8, client_ref="ref-1").json()
        self.assertTrue(first['success'] and again['success'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(first['invoice_number'], again['invoice_number'])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock(), 3)
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, D("80.00"))
        # The replayed receipt shows the same single line of 8, not 3 batch rows.
        self.assertEqual([(l['qty'], l['line_total']) for l in again['lines']], [(8, 80.0)])
        self.assert_tallies()

    def test_retry_returns_original_even_if_retry_payload_differs(self):
        self.sell(2, client_ref="ref-2")
        again = self.sell(5, client_ref="ref-2").json()
        self.assertTrue(again['duplicate'])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock(), 9)

    def test_different_checkouts_are_different_sales(self):
        self.sell(2, client_ref="a")
        self.sell(2, client_ref="b")
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(self.stock(), 7)
        self.assert_tallies()

    def test_another_cashiers_key_is_not_replayed(self):
        self.sell(2, client_ref="shared")
        self.client.force_login(self.cashier2)
        self.open_session(self.cashier2)
        self.assert_rejected(self.sell(2, client_ref="shared"))
        self.assertEqual(Sale.objects.count(), 1)

    def test_bad_keys_are_refused(self):
        self.assert_rejected(self.sell(1, client_ref="x" * 65))
        self.assert_rejected(self.sell(1, client_ref=123))
        self.assertFalse(Sale.objects.exists())

    def test_failed_attempt_does_not_burn_the_key(self):
        self.assert_rejected(self.sell(50, client_ref="retry-me"))
        self.assertTrue(self.sell(2, client_ref="retry-me").json()['success'])
        self.assertEqual(Sale.objects.count(), 1)


# =============================================================================
# Payments, change and credit
# =============================================================================
class PaymentTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)
        self.session = self.open_session(self.cashier)

    def drawer(self):
        self.session.refresh_from_db()
        return self.session.total_cash_sales, self.session.total_momo_sales, self.session.total_card_sales

    def test_cash_with_change_nets_into_the_drawer(self):
        body = self.sell(3, payments=[{'method': 'CASH', 'amount': '50'}]).json()
        self.assertEqual(body['change_due'], 20.0)
        sale = Sale.objects.get(pk=body['sale_id'])
        self.assertEqual(sale.amount_paid, D("30.00"))
        self.assertTrue(sale.payments.filter(amount=D("-20.00"), reference_id='CHANGE GIVEN').exists())
        self.assertEqual(self.drawer(), (D("30.00"), D("0.00"), D("0.00")))
        self.assert_tallies()

    def test_momo_over_the_bill_is_refused(self):
        # Typing 300 instead of 30 on MoMo used to "give" 270 cash change and
        # leave the drawer 270 short at close.
        self.assert_rejected(self.sell(3, payments=[{'method': 'MOMO', 'amount': '300'}]), 'change')
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(self.drawer(), (D("0.00"), D("0.00"), D("0.00")))

    def test_card_over_the_bill_is_refused(self):
        self.assert_rejected(self.sell(1, payments=[{'method': 'CARD', 'amount': '10.01'}]), 'change')

    def test_split_with_change_from_the_cash_part(self):
        # Bill 120: 50 cash + 100 momo -> 30 change, all from the 50 cash.
        body = self.sell(12 - 1, payments=[{'method': 'CASH', 'amount': '50'},
                                           {'method': 'MOMO', 'amount': '100'}]).json()
        # 11 x 10 = 110: 150 paid, 40 change (<= 50 cash).
        self.assertTrue(body['success'], body)
        self.assertEqual(body['change_due'], 40.0)
        self.assertEqual(self.drawer(), (D("10.00"), D("100.00"), D("0.00")))
        self.assert_tallies()

    def test_split_where_change_exceeds_cash_is_refused(self):
        self.assert_rejected(self.sell(3, payments=[{'method': 'CASH', 'amount': '5'},
                                                    {'method': 'MOMO', 'amount': '40'}]), 'change')

    def test_exact_split_cash_momo_card(self):
        body = self.sell(6, payments=[{'method': 'CASH', 'amount': '10'}, {'method': 'MOMO', 'amount': '20'},
                                      {'method': 'CARD', 'amount': '30'}]).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(self.drawer(), (D("10.00"), D("20.00"), D("30.00")))
        self.assert_tallies()

    def test_bank_and_cheque_do_not_touch_the_drawer(self):
        body = self.sell(2, payments=[{'method': 'BANK', 'amount': '20'}]).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(self.drawer(), (D("0.00"), D("0.00"), D("0.00")))
        self.assert_tallies()

    def test_store_credit_is_not_money(self):
        self.assert_rejected(self.sell(2, payments=[{'method': 'CREDIT', 'amount': '20'}],
                                       customer_id=self.customer.id), 'credit')
        self.assertFalse(Sale.objects.exists())

    def test_unpaid_balance_needs_a_customer(self):
        self.assert_rejected(self.sell(2, payments=[{'method': 'CASH', 'amount': '5'}]), 'customer')
        self.assert_rejected(self.sell(2, payments=[]), 'customer')
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(self.stock(), 11)

    def test_credit_sale_to_a_customer(self):
        body = self.sell(2, payments=[{'method': 'CASH', 'amount': '5'}], customer_id=self.customer.id).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['balance_due'], 15.0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.total_spent, D("20.00"))
        self.assertEqual(self.customer.total_visits, 1)
        self.assert_tallies()

    def test_full_credit_with_no_money_at_all(self):
        body = self.sell(2, payments=[], customer_id=self.customer.id).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['amount_paid'], 0.0)
        self.assertEqual(self.drawer(), (D("0.00"), D("0.00"), D("0.00")))
        self.assert_tallies()

    def test_another_shops_customer_cannot_be_billed(self):
        self.assert_rejected(self.sell(2, payments=[], customer_id=self.far_customer.id), 'customer')
        self.far_customer.refresh_from_db()
        self.assertEqual(self.far_customer.total_spent, D("0.00"))

    def test_owner_may_bill_any_shops_customer(self):
        self.client.force_login(self.owner)
        self.open_session(self.owner)
        body = self.sell(1, payments=[], customer_id=self.far_customer.id).json()
        self.assertTrue(body['success'], body)

    def test_unknown_customer_is_refused(self):
        self.assert_rejected(self.sell(1, customer_id=999999), 'customer')

    def test_bad_payment_amounts_are_refused(self):
        for amount in ("abc", "NaN", "Infinity", "-5", "1e20", True, [1]):
            resp = self.sell(1, payments=[{'method': 'CASH', 'amount': amount}])
            self.assertEqual(resp.status_code, 400, (amount, resp.content))
        self.assertFalse(Sale.objects.exists())

    def test_bad_payment_shapes_are_refused(self):
        for payments in ("cash", [1], [{'method': 'BITCOIN', 'amount': '10'}], {'method': 'CASH'}):
            resp = self.sell(1, payments=payments)
            self.assertEqual(resp.status_code, 400, (payments, resp.content))

    def test_thousands_separator_is_understood(self):
        body = self.sell(1, product=self.oil, payments=[{'method': 'CASH', 'amount': '1,000.00'}]).json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['change_due'], 990.0)

    def test_amounts_that_drift_in_floats_are_exact(self):
        # 3 x 33.33 = 99.99 exactly (in floats it is 99.99000000000001).
        body = self.sell(3, product=self.filter, payments=[{'method': 'CASH', 'amount': 99.99}]).json()
        self.assertTrue(body['success'], body)
        sale = Sale.objects.get(pk=body['sale_id'])
        self.assertEqual(sale.total_amount, D("99.99"))
        self.assertEqual(sale.change_due, D("0.00"))
        self.assertEqual(sale.subtotal + sale.total_tax, sale.total_amount)
        self.assert_tallies()

    def test_tax_split_always_adds_back_to_the_bill(self):
        for qty in (1, 2, 3, 7, 13):
            body = self.sell(qty, product=self.filter,
                             payments=[{'method': 'CASH', 'amount': str(D("33.33") * qty)}]).json()
            sale = Sale.objects.get(pk=body['sale_id'])
            self.assertEqual(sale.subtotal + sale.total_tax, sale.total_amount, qty)

    def test_stale_price_on_the_page_is_refused(self):
        self.assert_rejected(self.post_sale([{'id': self.oil.id, 'qty': 1, 'price': '9.00'}],
                                            [{'method': 'CASH', 'amount': '10'}]), 'out of date')

    def test_no_open_register_no_sale(self):
        self.session.status = RegisterSession.Status.CLOSED
        self.session.save()
        self.assert_rejected(self.sell(1), 'register')
        self.assertEqual(self.stock(), 11)

    def test_a_busy_day_tallies(self):
        pays = [
            [{'method': 'CASH', 'amount': '100'}],
            [{'method': 'MOMO', 'amount': '20'}],
            [{'method': 'CASH', 'amount': '5'}, {'method': 'CARD', 'amount': '5'}],
        ]
        for i in range(9):
            self.sell(1 + (i % 2), product=self.filter if i % 3 else self.oil, payments=pays[i % 3],
                      customer_id=self.customer.id)
        self.assert_tallies()


# =============================================================================
# Opening / closing the drawer
# =============================================================================
class RegisterTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)

    def test_open_with_a_bad_float_is_refused(self):
        for bad in ("abc", "-10", "NaN"):
            self.client.post(reverse('sales:pos'), {'opening_balance': bad})
        self.assertFalse(RegisterSession.objects.exists())

    def test_open_with_blank_float_is_zero(self):
        self.client.post(reverse('sales:pos'), {'opening_balance': ''})
        self.assertEqual(RegisterSession.objects.get().opening_balance, D("0.00"))

    def test_opening_twice_keeps_one_drawer(self):
        self.client.post(reverse('sales:pos'), {'opening_balance': '50'})
        self.client.post(reverse('sales:pos'), {'opening_balance': '70'})
        self.assertEqual(RegisterSession.objects.filter(status='OPEN').count(), 1)

    def test_close_counts_sales_and_matches(self):
        session = self.open_session(self.cashier, "100.00")
        self.sell(3, payments=[{'method': 'CASH', 'amount': '50'}])        # +30 net
        self.sell(2, payments=[{'method': 'MOMO', 'amount': '20'}])        # not cash
        self.client.post(reverse('sales:close_register'), {'actual_cash': '130.00'})
        session.refresh_from_db()
        self.assertEqual(session.closing_balance_expected, D("130.00"))
        self.assertEqual(session.status, RegisterSession.Status.CLOSED)
        self.assert_tallies()

    def test_close_records_a_shortage(self):
        session = self.open_session(self.cashier, "0")
        self.sell(1)
        self.client.post(reverse('sales:close_register'), {'actual_cash': '7'})
        session.refresh_from_db()
        self.assertEqual(session.status, RegisterSession.Status.DISCREPANCY)
        self.assertEqual(session.discrepancy, D("-3.00"))

    def test_mistyped_count_does_not_close_the_shift(self):
        # A typo used to become 0.00 and close the shift as a fake shortage.
        session = self.open_session(self.cashier)
        for bad in ("", "abc", "-1"):
            self.client.post(reverse('sales:close_register'), {'actual_cash': bad})
            session.refresh_from_db()
            self.assertEqual(session.status, RegisterSession.Status.OPEN, bad)


# =============================================================================
# Settling arrears
# =============================================================================
class SettlementTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)
        self.session = self.open_session(self.cashier)
        body = self.sell(2, payments=[{'method': 'CASH', 'amount': '5'}], customer_id=self.customer.id).json()
        self.sale = Sale.objects.get(pk=body['sale_id'])      # 20 bill, owes 15

    def pay(self, **data):
        return self.client.post(reverse('sales:add_payment', args=[self.sale.id]), data)

    def test_exact_settlement(self):
        self.pay(payment_method='MOMO', amount='15')
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.balance_remaining, D("0.00"))
        self.assert_tallies()

    def test_part_settlement(self):
        self.pay(payment_method='CASH', amount='4.50')
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.balance_remaining, D("10.50"))
        self.assert_tallies()

    def test_cash_overpayment_gives_change(self):
        self.pay(payment_method='CASH', amount='20')
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("20.00"))
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, D("20.00"))   # 5 + 15 net
        self.assert_tallies()

    def test_momo_overpayment_is_refused(self):
        self.pay(payment_method='MOMO', amount='50')
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("5.00"))
        self.assertFalse(self.sale.payments.filter(is_settlement=True).exists())

    def test_paying_twice_cannot_double_count(self):
        self.pay(payment_method='CASH', amount='15')
        self.pay(payment_method='CASH', amount='15')   # double-submitted form
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("20.00"))
        self.assertEqual(self.sale.payments.filter(is_settlement=True).count(), 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, D("20.00"))
        self.assert_tallies()

    def test_bad_amounts_are_refused_without_crashing(self):
        for amount in ("abc", "NaN", "-5", "", "0"):
            resp = self.pay(payment_method='CASH', amount=amount)
            self.assertEqual(resp.status_code, 302, amount)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("5.00"))

    def test_bad_split_is_refused_without_crashing(self):
        resp = self.pay(payment_method='SPLIT', split_cash='abc', split_momo='5')
        self.assertEqual(resp.status_code, 302)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("5.00"))

    def test_store_credit_does_not_settle_debt(self):
        self.pay(payment_method='CREDIT', amount='15')
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.balance_remaining, D("15.00"))

    def test_refunded_sale_takes_no_payments(self):
        self.sale.status = Sale.Status.REFUNDED
        self.sale.save()
        self.pay(payment_method='CASH', amount='15')
        self.assertFalse(self.sale.payments.filter(is_settlement=True).exists())

    def test_settlement_goes_into_todays_drawer(self):
        self.session.status = RegisterSession.Status.CLOSED
        self.session.save()
        today = self.open_session(self.cashier, "0")
        self.pay(payment_method='CASH', amount='15')
        row = self.sale.payments.get(is_settlement=True)
        self.assertEqual(row.register_session_id, today.id)
        today.refresh_from_db()
        self.assertEqual(today.total_cash_sales, D("15.00"))

    def test_other_shops_staff_cannot_take_payment(self):
        self.client.force_login(self.far_cashier)
        self.open_session(self.far_cashier)
        self.pay(payment_method='CASH', amount='15')
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("5.00"))


# =============================================================================
# Refunds
# =============================================================================
class RefundTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)
        self.cash_session = self.open_session(self.cashier)
        self.client.force_login(self.manager)
        self.mgr_session = self.open_session(self.manager, "0")

    def make_sale(self, qty, payments, customer=None):
        self.client.force_login(self.cashier)
        body = self.sell(qty, payments=payments, customer_id=customer.id if customer else None).json()
        self.assertTrue(body['success'], body)
        self.client.force_login(self.manager)
        return Sale.objects.get(pk=body['sale_id'])

    def refund(self, sale, items, method='CASH'):
        return self.client.post(reverse('sales:refund', args=[sale.id]), {
            'refund_items': [i.id for i in items], 'refund_method': method, 'reason': 'Return'})

    def test_full_refund_of_a_cash_sale(self):
        sale = self.make_sale(8, [{'method': 'CASH', 'amount': '80'}])
        self.refund(sale, sale.items.all())
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.REFUNDED)
        self.assertEqual((sale.total_amount, sale.amount_paid), (D("0.00"), D("0.00")))
        self.assertEqual(self.stock(), 11)
        self.mgr_session.refresh_from_db()
        self.assertEqual(self.mgr_session.total_cash_sales, D("-80.00"))
        self.assert_tallies()

    def test_refunding_the_same_line_twice_restocks_once(self):
        sale = self.make_sale(2, [{'method': 'CASH', 'amount': '20'}])
        item = sale.items.first()
        self.refund(sale, [item])
        self.refund(sale, [item])
        self.assertEqual(self.stock(), 11)
        self.assertEqual(sale.payments.filter(reference_id__startswith='REFUND').count(), 1)
        self.assert_tallies()

    def test_refund_of_an_unpaid_credit_sale_pays_nothing_out(self):
        # Used to hand the full value out of the drawer for goods never paid for.
        sale = self.make_sale(3, [], customer=self.customer)
        self.refund(sale, sale.items.all())
        sale.refresh_from_db()
        self.assertEqual(sale.balance_remaining, D("0.00"))
        self.assertFalse(sale.payments.filter(amount__lt=0).exists())
        self.mgr_session.refresh_from_db()
        self.assertEqual(self.mgr_session.total_cash_sales, D("0.00"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.total_spent, D("0.00"))
        self.assert_tallies()

    def test_partial_refund_on_a_part_paid_sale_clears_debt_first(self):
        # 3 + 3 + 5 batches: a qty-4 sale has lines of 3 and 1.
        sale = self.make_sale(4, [{'method': 'CASH', 'amount': '25'}], customer=self.customer)  # owes 15
        small = sale.items.get(quantity=1)
        self.refund(sale, [small])                                         # 10 back
        sale.refresh_from_db()
        self.assertEqual(sale.total_amount, D("30.00"))
        self.assertEqual(sale.amount_paid, D("25.00"))
        self.assertEqual(sale.balance_remaining, D("5.00"))                # debt 15 -> 5
        self.assertEqual(sale.status, Sale.Status.PARTIAL_REFUND)
        self.assert_tallies()

    def test_momo_refund_comes_out_of_momo(self):
        sale = self.make_sale(1, [{'method': 'MOMO', 'amount': '10'}])
        self.refund(sale, sale.items.all(), method='MOMO')
        self.mgr_session.refresh_from_db()
        self.assertEqual((self.mgr_session.total_cash_sales, self.mgr_session.total_momo_sales),
                         (D("0.00"), D("-10.00")))
        self.assert_tallies()

    def test_bad_refund_methods_are_refused(self):
        sale = self.make_sale(1, [{'method': 'CASH', 'amount': '10'}])
        for method in ('CREDIT', 'BITCOIN', ''):
            self.refund(sale, sale.items.all(), method=method)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertEqual(self.stock(), 10)

    def test_no_items_selected_changes_nothing(self):
        sale = self.make_sale(1, [{'method': 'CASH', 'amount': '10'}])
        self.refund(sale, [])
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.COMPLETED)

    def test_items_from_another_sale_are_ignored(self):
        a = self.make_sale(1, [{'method': 'CASH', 'amount': '10'}])
        b = self.make_sale(1, [{'method': 'CASH', 'amount': '10'}])
        self.refund(a, b.items.all())
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), (Sale.Status.COMPLETED, Sale.Status.COMPLETED))
        self.assertEqual(self.stock(), 9)

    def test_refund_then_settle_then_refund_tallies(self):
        sale = self.make_sale(4, [{'method': 'CASH', 'amount': '10'}], customer=self.customer)  # owes 30
        self.refund(sale, [sale.items.get(quantity=1)])                    # owes 20
        self.client.post(reverse('sales:add_payment', args=[sale.id]),
                         {'payment_method': 'CASH', 'amount': '20'})       # settled
        self.refund(sale, [sale.items.get(quantity=3)])                    # 30 back
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.REFUNDED)
        self.assertEqual((sale.total_amount, sale.amount_paid), (D("0.00"), D("0.00")))
        self.assertEqual(self.stock(), 11)
        self.assert_tallies()


# =============================================================================
# reconcile_sales: finding and repairing old damage
# =============================================================================
class ReconcileTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)
        self.session = self.open_session(self.cashier)

    def fixed(self, **kw):
        report = reconcile(fix=True, **kw)
        return {f.check for f in report.findings if f.fixed}

    def checks(self, report):
        return {f.check for f in report.findings}

    def test_clean_data_has_no_findings(self):
        self.sell(3, payments=[{'method': 'CASH', 'amount': '50'}])
        self.assertEqual(reconcile().findings, [])

    def test_legacy_missing_change_row_is_rebuilt(self):
        # Older checkouts: paid = tendered, change_due set, no CHANGE row.
        sale = Sale.objects.get(pk=self.sell(3, payments=[{'method': 'CASH', 'amount': '50'}]).json()['sale_id'])
        sale.payments.filter(amount__lt=0).delete()
        Sale.objects.filter(pk=sale.pk).update(amount_paid=D("50.00"))
        report = reconcile()
        self.assertIn('missing-change', self.checks(report))
        self.assertEqual(Sale.objects.get(pk=sale.pk).amount_paid, D("50.00"))   # dry run wrote nothing
        self.assertIn('missing-change', self.fixed())
        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, D("30.00"))
        change = sale.payments.get(amount=D("-20.00"))
        self.assertEqual(change.register_session_id, self.session.id)
        self.assertEqual(change.created_at, sale.created_at)
        self.assertEqual(reconcile().findings, [])                                # idempotent

    def test_amount_paid_drift_is_repaired_from_payment_rows(self):
        sale = Sale.objects.get(pk=self.sell(2).json()['sale_id'])
        Sale.objects.filter(pk=sale.pk).update(amount_paid=D("7.00"))
        self.assertIn('amount-paid', self.fixed())
        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, D("20.00"))

    def test_bill_drift_is_repaired_from_lines(self):
        sale = Sale.objects.get(pk=self.sell(2).json()['sale_id'])
        Sale.objects.filter(pk=sale.pk).update(total_amount=D("99.00"))
        self.assertIn('sale-total', self.fixed())
        sale.refresh_from_db()
        self.assertEqual(sale.total_amount, D("20.00"))
        self.assertEqual(reconcile().findings, [])

    def test_line_total_drift_is_repaired(self):
        sale = Sale.objects.get(pk=self.sell(2).json()['sale_id'])
        SaleItem.objects.filter(sale=sale).update(total_price=D("1.00"))
        self.assertIn('line-total', self.fixed())
        self.assertEqual(sum(i.total_price for i in sale.items.all()), D("20.00"))

    def test_status_drift_is_repaired(self):
        sale = Sale.objects.get(pk=self.sell(4).json()['sale_id'])     # lines 3 + 1
        line = sale.items.get(quantity=1)
        SaleItem.objects.filter(pk=line.pk).update(is_refunded=True)
        Sale.objects.filter(pk=sale.pk).update(total_amount=D("30.00"))
        SalePayment.objects.create(sale=sale, payment_method='CASH', amount=D("-10.00"),
                                   reference_id='REFUND - x', register_session=self.session)
        Sale.objects.filter(pk=sale.pk).update(amount_paid=D("30.00"))
        RegisterSession.objects.filter(pk=self.session.pk).update(total_cash_sales=D("30.00"))
        self.assertIn('status', self.fixed())
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.PARTIAL_REFUND)

    def test_open_drawer_counter_drift_is_repaired(self):
        self.sell(3)
        RegisterSession.objects.filter(pk=self.session.pk).update(total_cash_sales=D("999.00"))
        self.assertIn('drawer', self.fixed())
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, D("30.00"))

    def test_closed_drawer_only_changes_with_include_closed(self):
        self.sell(3)
        RegisterSession.objects.filter(pk=self.session.pk).update(
            total_cash_sales=D("10.00"), status='CLOSED', end_time=timezone.now(),
            closing_balance_expected=D("110.00"), closing_balance_actual=D("130.00"))
        report = reconcile(fix=True)
        drawer = [f for f in report.findings if f.check == 'drawer']
        self.assertTrue(drawer and not drawer[0].fixed)
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, D("10.00"))

        self.assertIn('drawer', self.fixed(include_closed=True))
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cash_sales, D("30.00"))
        self.assertEqual(self.session.closing_balance_expected, D("130.00"))
        self.assertEqual(self.session.status, RegisterSession.Status.CLOSED)   # count now matches

    def test_untagged_legacy_payments_are_linked_to_their_drawer(self):
        sale = Sale.objects.get(pk=self.sell(2, payments=[], customer_id=self.customer.id).json()['sale_id'])
        self.client.post(reverse('sales:add_payment', args=[sale.id]), {'payment_method': 'CASH', 'amount': '20'})
        SalePayment.objects.update(register_session=None)
        self.assertIn('payment-drawer', self.fixed())
        self.assertFalse(SalePayment.objects.filter(register_session__isnull=True).exists())
        self.assertEqual(reconcile().findings, [])

    def test_customer_spend_drift_is_repaired(self):
        self.sell(2, payments=[], customer_id=self.customer.id)
        Customer.objects.filter(pk=self.customer.pk).update(total_spent=D("0.00"))
        self.assertIn('customer-spend', self.fixed())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.total_spent, D("20.00"))

    def test_double_submitted_sale_is_flagged_not_touched(self):
        self.sell(4)
        self.sell(3)                 # different goods: not a duplicate
        self.sell(4)                 # same goods, seconds later: suspicious
        report = reconcile(fix=True)
        dupes = [f for f in report.findings if f.check == 'possible-duplicate']
        self.assertEqual(len(dupes), 1)
        self.assertFalse(dupes[0].fixed)
        self.assertEqual(Sale.objects.count(), 3)

    def test_same_goods_hours_apart_is_not_a_duplicate(self):
        self.sell(4)
        second = Sale.objects.get(pk=self.sell(4).json()['sale_id'])
        Sale.objects.filter(pk=second.pk).update(created_at=second.created_at + timedelta(hours=2))
        self.assertNotIn('possible-duplicate', self.checks(reconcile()))

    def test_over_refund_is_flagged_for_review(self):
        sale = Sale.objects.get(pk=self.sell(2, payments=[], customer_id=self.customer.id).json()['sale_id'])
        SalePayment.objects.create(sale=sale, payment_method='CASH', amount=D("-20.00"),
                                   reference_id='REFUND - old', register_session=self.session)
        findings = [f for f in reconcile(fix=True).findings if f.check == 'over-refunded']
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0].fixed)

    def test_negative_stock_is_flagged(self):
        StockBatch.objects.filter(pk=self.batches[0].pk).update(quantity=-2)
        self.assertIn('negative-stock', self.checks(reconcile()))

    def test_location_filter(self):
        self.sell(2)
        Sale.objects.update(total_amount=D("1.00"))
        self.assertEqual(self.checks(reconcile(location=self.other_loc)), set())
        self.assertIn('sale-total', self.checks(reconcile(location=self.loc)))

    def test_clear_debtors_dry_run_changes_nothing(self):
        self.sell(3, payments=[{'method': 'CASH', 'amount': '10'}], customer_id=self.customer.id)
        out = StringIO()
        call_command('clear_debtors', stdout=out)
        self.assertIn('DRY RUN', out.getvalue())
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock(), 8)

    def test_clear_debtors_deletes_debts_and_undoes_them(self):
        paid = self.sell(2)                                                     # fully paid: kept
        # A part-refunded debt: 4 units over two batches (lines 1 + 3); the 1 is refunded below.
        debt = Sale.objects.get(pk=self.sell(4, payments=[{'method': 'MOMO', 'amount': '5'}],
                                             customer_id=self.customer.id).json()['sale_id'])
        self.assertEqual(sorted(i.quantity for i in debt.items.all()), [1, 3])
        self.sell(3, payments=[{'method': 'CASH', 'amount': '10'}], customer_id=self.customer.id)
        self.sell(1, payments=[], customer_id=self.customer.id)
        self.client.force_login(self.manager)
        self.open_session(self.manager, "0")
        one = debt.items.order_by('quantity').first()
        self.client.post(reverse('sales:refund', args=[debt.id]),
                         {'refund_items': [one.id], 'refund_method': 'CASH', 'reason': 'x'})
        self.assertEqual(self.stock(), 11 - 2 - 3 - 1 - 4 + one.quantity)

        out = StringIO()
        call_command('clear_debtors', '--commit', stdout=out)
        out.getvalue().encode('ascii')

        self.assertEqual(list(Sale.objects.values_list('pk', flat=True)), [paid.json()['sale_id']])
        self.assertEqual(self.stock(), 11 - 2)                       # only the paid sale's units are gone
        self.session.refresh_from_db()
        self.assertEqual((self.session.total_cash_sales, self.session.total_momo_sales),
                         (D("20.00"), D("0.00")))                    # just the paid sale
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.total_spent, self.customer.total_visits), (D("0.00"), 0))
        self.assert_tallies()
        out = StringIO()
        call_command('clear_debtors', stdout=out)
        self.assertIn('No debtors', out.getvalue())

    def test_command_dry_run_then_commit(self):
        self.sell(2)
        Sale.objects.update(total_amount=D("1.00"))
        out = StringIO()
        call_command('reconcile_sales', stdout=out)
        self.assertIn('DRY RUN', out.getvalue())
        self.assertEqual(Sale.objects.get().total_amount, D("1.00"))
        out = StringIO()
        call_command('reconcile_sales', '--commit', stdout=out)
        self.assertIn('Repaired', out.getvalue())
        self.assertEqual(Sale.objects.get().total_amount, D("20.00"))
        out = StringIO()
        call_command('reconcile_sales', stdout=out)
        self.assertIn('Everything tallies', out.getvalue())
        out.getvalue().encode('ascii')   # Windows consoles: no characters they can't print


# =============================================================================
# A shift left open from an earlier day must be closed first
# =============================================================================
class StaleShiftTests(MoneyTestBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)
        self.session = self.open_session(self.cashier)

    def make_stale(self, session=None, days=1):
        session = session or self.session
        RegisterSession.objects.filter(pk=session.pk).update(start_time=timezone.now() - timedelta(days=days))

    def test_todays_shift_sells_normally(self):
        self.assertEqual(self.client.get(reverse('sales:pos')).status_code, 200)
        self.assertTrue(self.sell(1).json()['success'])

    def test_pos_sends_you_to_close_yesterdays_shift(self):
        self.make_stale()
        resp = self.client.get(reverse('sales:pos'))
        self.assertRedirects(resp, reverse('sales:close_register'), fetch_redirect_response=False)
        page = self.client.get(reverse('sales:close_register'))
        self.assertContains(page, 'This shift started on')

    def test_checkout_on_a_stale_shift_is_refused(self):
        # e.g. a POS tab left open overnight.
        self.make_stale()
        self.assert_rejected(self.sell(2), 'still open')
        self.assertEqual(self.stock(), 11)
        self.assertFalse(Sale.objects.exists())

    def test_retry_of_a_sale_made_before_midnight_still_returns_it(self):
        first = self.sell(2, client_ref='late-night').json()
        self.make_stale()
        again = self.sell(2, client_ref='late-night').json()
        self.assertTrue(again['success'] and again['duplicate'])
        self.assertEqual(again['invoice_number'], first['invoice_number'])

    def test_debt_payments_and_refunds_wait_for_the_close(self):
        sale = Sale.objects.get(pk=self.sell(2, payments=[], customer_id=self.customer.id).json()['sale_id'])
        self.make_stale()
        resp = self.client.post(reverse('sales:add_payment', args=[sale.id]), {'payment_method': 'CASH', 'amount': '20'})
        self.assertRedirects(resp, reverse('sales:close_register'), fetch_redirect_response=False)
        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, D("0.00"))

        self.client.force_login(self.manager)
        mgr = self.open_session(self.manager, "0")
        self.make_stale(mgr)
        resp = self.client.post(reverse('sales:refund', args=[sale.id]),
                                {'refund_items': [i.id for i in sale.items.all()], 'refund_method': 'CASH'})
        self.assertRedirects(resp, reverse('sales:close_register'), fetch_redirect_response=False)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.COMPLETED)

    def test_close_then_open_today(self):
        self.make_stale(days=3)
        self.client.post(reverse('sales:close_register'), {'actual_cash': '100'})
        self.session.refresh_from_db()
        self.assertNotEqual(self.session.status, RegisterSession.Status.OPEN)
        # Now the POS asks for today's float instead of reusing the old one.
        self.assertTemplateUsed(self.client.get(reverse('sales:pos')), 'sales/open_register.html')
        self.client.post(reverse('sales:pos'), {'opening_balance': '150'})
        today = RegisterSession.objects.get(status=RegisterSession.Status.OPEN)
        self.assertEqual(today.opening_balance, D("150.00"))
        self.assertTrue(self.sell(1).json()['success'])

    def test_distributor_sale_waits_for_the_close(self):
        self.client.force_login(self.manager)
        mgr = self.open_session(self.manager, "0")
        self.make_stale(mgr)
        self.oil.distributor_price = D("8.00")
        self.oil.save()
        dist = Customer.objects.create(phone_number="0249", first_name="Dist", location=self.loc,
                                       is_distributor=True)
        resp = self.client.post(reverse('customers:distributor_sell', args=[dist.id]),
                                {f'qty_{self.oil.id}': '2', 'payment_method': 'CASH', 'amount_paid': '16'})
        self.assertRedirects(resp, reverse('sales:close_register'), fetch_redirect_response=False)
        self.assertFalse(Sale.objects.exists())
