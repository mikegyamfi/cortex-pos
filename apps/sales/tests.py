import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.users.models import User
from apps.location.models import Location
from apps.products.models import Product
from apps.inventory.models import StockBatch
from apps.sales.models import Sale, SalePayment, RegisterSession


class POSTestBase(TestCase):
    """Shared fixtures: one shop, staff of each relevant role, one product with stock."""

    def setUp(self):
        self.loc = Location.objects.create(name="Main Shop", address="1 Test St")

        self.owner = User.objects.create_user(
            username="owner", password="pw", role="OWNER", assigned_location=self.loc
        )
        self.manager = User.objects.create_user(
            username="manager", password="pw", role="MANAGER", assigned_location=self.loc
        )
        self.cashier = User.objects.create_user(
            username="cashier", password="pw", role="CASHIER", assigned_location=self.loc
        )
        self.salesperson = User.objects.create_user(
            username="seller", password="pw", role="SALESPERSON", assigned_location=self.loc
        )

        self.product = Product.objects.create(
            name="Cola 500ml", sku="COLA500", cost_price=Decimal("6.00"),
            selling_price=Decimal("10.00"),  # no wholesale -> wholesale falls back to retail
        )
        self.batch = StockBatch.objects.create(
            product=self.product, location=self.loc, quantity=50, cost_price=Decimal("6.00")
        )

    def open_session(self, user, opening="100.00"):
        return RegisterSession.objects.create(
            user=user, location=self.loc, opening_balance=Decimal(opening),
            status=RegisterSession.Status.OPEN,
        )

    def post_sale(self, payload):
        return self.client.post(
            reverse("sales:process_sale"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    @staticmethod
    def payments_sum(sale):
        return sum((p.amount for p in sale.payments.all()), Decimal("0.00"))


class ProcessSaleFinancialIntegrityTests(POSTestBase):

    def test_total_is_computed_server_side_and_ignores_tampered_client_total(self):
        self.client.force_login(self.cashier)
        self.open_session(self.cashier)

        resp = self.post_sale({
            "cart": [{"id": self.product.id, "qty": 2, "price": "10.00"}],
            "payments": [{"method": "CASH", "amount": "20.00"}],
            "customer_id": None,
            "total_amount": "999999.00",  # tampered — must be ignored
        })
        body = resp.json()
        self.assertTrue(body["success"], body)

        sale = Sale.objects.get(id=body["sale_id"])
        # Authoritative total = 2 x 10.00, NOT the tampered 999999.
        self.assertEqual(sale.total_amount, Decimal("20.00"))
        self.assertEqual(sale.amount_paid, Decimal("20.00"))
        self.assertEqual(sale.change_due, Decimal("0.00"))

        # Stock deducted; payments reconcile to the bill.
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 48)
        self.assertEqual(self.payments_sum(sale), Decimal("20.00"))

    def test_invalid_price_is_rejected(self):
        self.client.force_login(self.cashier)
        self.open_session(self.cashier)

        resp = self.post_sale({
            "cart": [{"id": self.product.id, "qty": 1, "price": "5.00"}],  # not retail/wholesale
            "payments": [{"method": "CASH", "amount": "5.00"}],
            "customer_id": None,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])
        self.assertEqual(Sale.objects.count(), 0)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 50)  # untouched

    def test_oversell_is_blocked(self):
        self.client.force_login(self.cashier)
        self.open_session(self.cashier)

        resp = self.post_sale({
            "cart": [{"id": self.product.id, "qty": 999, "price": "10.00"}],
            "payments": [{"method": "CASH", "amount": "9990.00"}],
            "customer_id": None,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Insufficient stock", resp.json()["message"])
        self.assertEqual(Sale.objects.count(), 0)

    def test_change_is_recorded_and_drawer_reflects_net_cash(self):
        self.client.force_login(self.cashier)
        session = self.open_session(self.cashier)

        resp = self.post_sale({
            "cart": [{"id": self.product.id, "qty": 1, "price": "10.00"}],
            "payments": [{"method": "CASH", "amount": "20.00"}],  # 10 change
            "customer_id": None,
        })
        body = resp.json()
        self.assertTrue(body["success"])
        sale = Sale.objects.get(id=body["sale_id"])
        self.assertEqual(sale.change_due, Decimal("10.00"))
        self.assertEqual(sale.amount_paid, Decimal("10.00"))  # net of change

        # Payments sum to the bill (20 tendered - 10 change returned).
        self.assertEqual(self.payments_sum(sale), Decimal("10.00"))
        # Drawer cash gained = 10 (20 in, 10 out).
        session.refresh_from_db()
        self.assertEqual(session.total_cash_sales, Decimal("10.00"))

    def test_sale_requires_open_session(self):
        self.client.force_login(self.cashier)  # no session opened
        resp = self.post_sale({
            "cart": [{"id": self.product.id, "qty": 1, "price": "10.00"}],
            "payments": [{"method": "CASH", "amount": "10.00"}],
            "customer_id": None,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)


class RefundReconciliationTests(POSTestBase):

    def _make_sale(self):
        self.client.force_login(self.cashier)
        self.open_session(self.cashier)
        resp = self.post_sale({
            "cart": [{"id": self.product.id, "qty": 2, "price": "10.00"}],
            "payments": [{"method": "CASH", "amount": "20.00"}],
            "customer_id": None,
        })
        return Sale.objects.get(id=resp.json()["sale_id"])

    def test_full_refund_restocks_and_reconciles(self):
        sale = self._make_sale()
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 48)

        # Manager refunds from their own open drawer.
        self.client.force_login(self.manager)
        self.open_session(self.manager)
        item_ids = list(sale.items.values_list("id", flat=True))

        resp = self.client.post(
            reverse("sales:refund", args=[sale.id]),
            data={"refund_items": item_ids, "refund_method": "CASH", "reason": "Return"},
        )
        self.assertEqual(resp.status_code, 302)

        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.REFUNDED)
        self.assertEqual(sale.amount_paid, Decimal("0.00"))

        # Stock fully restored.
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 50)

        # A negative payment was written so payments still reconcile to net revenue (0).
        self.assertEqual(self.payments_sum(sale), Decimal("0.00"))

    def test_cashier_cannot_refund(self):
        sale = self._make_sale()
        self.client.force_login(self.cashier)
        resp = self.client.post(
            reverse("sales:refund", args=[sale.id]),
            data={"refund_items": list(sale.items.values_list("id", flat=True))},
        )
        # role_required redirects unauthorized users away; the sale is untouched.
        self.assertEqual(resp.status_code, 302)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.COMPLETED)


class ArrearsTests(POSTestBase):

    def _credit_sale(self, paid):
        """Create a sale of 2 x 10 = 20, paying `paid` (a credit sale if < 20)."""
        from apps.customers.models import Customer
        cust = Customer.objects.create(phone_number="0240000001", first_name="Ama")
        self.client.force_login(self.cashier)
        self.open_session(self.cashier)
        resp = self.post_sale({
            "cart": [{"id": self.product.id, "qty": 2, "price": "10.00"}],
            "payments": [{"method": "CASH", "amount": str(paid)}],
            "customer_id": cust.id,
        })
        return Sale.objects.get(id=resp.json()["sale_id"]), cust

    def test_arrears_lists_outstanding_balance(self):
        sale, cust = self._credit_sale("12.00")  # owes 8
        self.assertEqual(sale.balance_remaining, Decimal("8.00"))

        resp = self.client.get(reverse("sales:arrears"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["grand_total"], Decimal("8.00"))
        self.assertEqual(resp.context["debtor_count"], 1)
        self.assertEqual(resp.context["debtors"][0]["customer"].id, cust.id)

    def test_fully_paid_sale_not_in_arrears(self):
        self._credit_sale("20.00")  # paid in full
        resp = self.client.get(reverse("sales:arrears"))
        self.assertEqual(resp.context["grand_total"], Decimal("0.00"))
        self.assertEqual(resp.context["debtor_count"], 0)

    def test_partial_refund_on_credit_sale_keeps_balance_correct(self):
        from apps.inventory.models import StockBatch
        # Two single-unit batches -> a qty-2 sale yields TWO 10.00 line items,
        # so we can refund exactly one of them.
        self.batch.quantity = 0
        self.batch.save()
        StockBatch.objects.create(product=self.product, location=self.loc, quantity=1, cost_price=Decimal("6"))
        StockBatch.objects.create(product=self.product, location=self.loc, quantity=1, cost_price=Decimal("6"))

        sale, cust = self._credit_sale("12.00")  # 20 bill, paid 12, owes 8
        self.assertEqual(sale.items.count(), 2)

        self.client.force_login(self.manager)
        self.open_session(self.manager)
        one_item = sale.items.first()  # qty 1, total 10
        self.client.post(
            reverse("sales:refund", args=[sale.id]),
            data={"refund_items": [one_item.id], "refund_method": "CASH", "reason": "Return"},
        )
        sale.refresh_from_db()
        # total 20 -> 10, paid 12 -> 2; they still owe 8 for the kept item.
        self.assertEqual(sale.total_amount, Decimal("10.00"))
        self.assertEqual(sale.amount_paid, Decimal("2.00"))
        self.assertEqual(sale.balance_remaining, Decimal("8.00"))

    def test_salesperson_can_view_arrears(self):
        self.client.force_login(self.salesperson)
        self.assertEqual(self.client.get(reverse("sales:arrears")).status_code, 200)

    def _cashier_session(self):
        return RegisterSession.objects.get(
            user=self.cashier, location=self.loc, status=RegisterSession.Status.OPEN
        )

    def test_split_settlement_clears_debt_and_logs_payments(self):
        sale, cust = self._credit_sale("5.00")  # 20 bill, paid 5, owes 15
        self.assertEqual(sale.balance_remaining, Decimal("15.00"))

        # Cashier settles the 15 across MoMo + Cash (drawer already open).
        resp = self.client.post(
            reverse("sales:add_payment", args=[sale.id]),
            data={"payment_method": "SPLIT", "split_momo": "10", "split_cash": "5"},
        )
        self.assertEqual(resp.status_code, 302)

        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, Decimal("20.00"))
        self.assertEqual(sale.balance_remaining, Decimal("0.00"))

        # Two settlement rows were written (momo + cash), totalling 15.
        settle = SalePayment.objects.filter(sale=sale, is_settlement=True)
        self.assertEqual(settle.count(), 2)
        self.assertEqual(sum(p.amount for p in settle), Decimal("15.00"))

        # The drawer reflects the split: +10 momo.
        self.assertEqual(self._cashier_session().total_momo_sales, Decimal("10.00"))

    def test_settlement_overpayment_returns_change(self):
        sale, cust = self._credit_sale("5.00")  # owes 15
        self.client.post(
            reverse("sales:add_payment", args=[sale.id]),
            data={"payment_method": "CASH", "amount": "20"},  # 15 owed, 5 change
        )
        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, Decimal("20.00"))   # capped at the bill
        self.assertEqual(sale.change_due, Decimal("5.00"))
        # A negative CHANGE GIVEN row exists and is NOT a settlement.
        self.assertTrue(SalePayment.objects.filter(sale=sale, amount=Decimal("-5.00"), is_settlement=False).exists())

    def test_arrears_payment_log_lists_settlements(self):
        sale, cust = self._credit_sale("5.00")
        self.client.post(reverse("sales:add_payment", args=[sale.id]),
                         data={"payment_method": "CASH", "amount": "15"})
        resp = self.client.get(reverse("sales:arrears_log"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_collected"], Decimal("15.00"))
        self.assertEqual(resp.context["count"], 1)


class RoleGuardingTests(POSTestBase):

    def test_salesperson_blocked_from_inventory(self):
        self.client.force_login(self.salesperson)
        resp = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("dashboard:index"))

    def test_salesperson_blocked_from_finance(self):
        self.client.force_login(self.salesperson)
        resp = self.client.get(reverse("finance:profit_loss"))
        self.assertEqual(resp.status_code, 302)

    def test_cashier_blocked_from_product_create(self):
        self.client.force_login(self.cashier)
        resp = self.client.get(reverse("products:product_create"))
        self.assertEqual(resp.status_code, 302)

    def test_owner_allowed_on_finance(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("finance:profit_loss"))
        self.assertEqual(resp.status_code, 200)

    def test_salesperson_can_reach_pos(self):
        self.client.force_login(self.salesperson)
        resp = self.client.get(reverse("sales:pos"))
        # No open session yet -> shown the open-register screen, but access is granted.
        self.assertEqual(resp.status_code, 200)

    def test_product_search_searches_whole_catalogue(self):
        # A product is findable via the server API regardless of the initial grid.
        Product.objects.create(
            name="Zebra Energy Drink", sku="ZEB1",
            cost_price=Decimal("2.00"), selling_price=Decimal("5.00"),
        )
        self.client.force_login(self.cashier)
        resp = self.client.get(reverse("sales:product_search"), {"q": "zebra"})
        self.assertEqual(resp.status_code, 200)
        names = [r["name"] for r in resp.json()["results"]]
        self.assertIn("Zebra Energy Drink", names)
