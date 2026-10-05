from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale, SalePayment
from apps.sales.services import record_refund, record_sale, record_settlement, resolve_cart
from apps.users.models import User

D = Decimal


class ReportTestBase(TestCase):
    """Two shops, each with its own same-named product, and open drawers."""

    def setUp(self):
        self.shop_a = Location.objects.create(name="Shop A", address="a")
        self.shop_b = Location.objects.create(name="Shop B", address="b")
        self.owner = User.objects.create_user(username="own", password="pw", role="OWNER",
                                              assigned_location=self.shop_a)
        self.manager_a = User.objects.create_user(username="mgr", password="pw", role="MANAGER",
                                                  assigned_location=self.shop_a)
        self.cashier_a = User.objects.create_user(username="ca", password="pw", role="CASHIER",
                                                  assigned_location=self.shop_a)
        self.cashier_b = User.objects.create_user(username="cb", password="pw", role="CASHIER",
                                                  assigned_location=self.shop_b)
        self.sessions = {
            u.pk: RegisterSession.objects.create(user=u, location=u.assigned_location, opening_balance=0)
            for u in (self.cashier_a, self.cashier_b, self.manager_a)
        }
        # The same product in both shops: same name, separate records.
        self.oil_a = self.product(self.shop_a, "Engine Oil", "OIL", "10.00", batches=(3, 3, 20))
        self.oil_b = self.product(self.shop_b, "Engine Oil", "OIL", "12.00", batches=(20,))

    def product(self, shop, name, sku, price, batches=(100,)):
        p = Product.objects.create(name=name, sku=sku, location=shop, cost_price=D("6.00"),
                                   selling_price=D(price))
        for qty in batches:
            StockBatch.objects.create(product=p, location=shop, quantity=qty, cost_price=D("6.00"))
        return p

    def sell(self, cashier, product, qty, days_ago=0):
        resolved, per_product = resolve_cart([{'id': product.id, 'qty': qty}], cashier.assigned_location)
        sale = record_sale(location=cashier.assigned_location, user=cashier,
                           session=self.sessions[cashier.pk], resolved=resolved,
                           qty_per_product=per_product,
                           payments=[{'method': 'CASH', 'amount': str(product.selling_price * qty)}])
        if days_ago:
            Sale.objects.filter(pk=sale.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return sale

    def report(self, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse('dashboard:reports'), params)
        self.assertEqual(resp.status_code, 200)
        return resp

    def rows(self, resp):
        return [(r['product__name'], r['product__location__name'], r['units'])
                for r in resp.context['page_obj']]


class BusinessReportTests(ReportTestBase):
    """The Business Reports page: products sold, totals, windows, scoping."""

    # ---- "they sold 8 but it shows 11" ---------------------------------------
    def test_same_named_products_in_two_shops_are_not_merged(self):
        self.sell(self.cashier_a, self.oil_a, 8)   # 3 + 3 + 2 over three batches
        self.sell(self.cashier_b, self.oil_b, 3)
        rows = self.rows(self.report(self.owner))
        self.assertCountEqual(rows, [("Engine Oil", "Shop A", 8), ("Engine Oil", "Shop B", 3)])

    def test_owner_can_narrow_to_one_shop(self):
        self.sell(self.cashier_a, self.oil_a, 8)
        self.sell(self.cashier_b, self.oil_b, 3)
        resp = self.report(self.owner, location=self.shop_a.id)
        self.assertEqual(self.rows(resp), [("Engine Oil", "Shop A", 8)])
        self.assertEqual(resp.context['revenue_today'], D("80.00"))

    def test_manager_only_sees_their_shop(self):
        self.sell(self.cashier_a, self.oil_a, 8)
        self.sell(self.cashier_b, self.oil_b, 3)
        resp = self.report(self.manager_a, location=self.shop_b.id)   # ignored for managers
        self.assertEqual(self.rows(resp), [("Engine Oil", "Shop A", 8)])
        self.assertEqual(resp.context['revenue_30d'], D("80.00"))

    def test_same_name_twice_in_one_shop_stays_two_rows(self):
        other = self.product(self.shop_a, "Engine Oil", "OIL-2", "15.00")
        self.sell(self.cashier_a, self.oil_a, 2)
        self.sell(self.cashier_a, other, 5)
        rows = self.rows(self.report(self.manager_a))
        self.assertCountEqual(rows, [("Engine Oil", "Shop A", 2), ("Engine Oil", "Shop A", 5)])

    def test_refunded_units_are_not_counted(self):
        sale = self.sell(self.cashier_a, self.oil_a, 8)               # lines 3, 3, 2
        line = sale.items.get(quantity=2)
        record_refund(sale=sale, user=self.manager_a, session=self.sessions[self.manager_a.pk],
                      item_ids=[line.id], method='CASH', reason='x')
        resp = self.report(self.manager_a)
        self.assertEqual(self.rows(resp), [("Engine Oil", "Shop A", 6)])
        self.assertEqual(resp.context['totals']['revenue'], D("60.00"))
        self.assertEqual(resp.context['revenue_today'], D("60.00"))

    # ---- pagination and totals ----------------------------------------------
    def test_every_product_sold_is_listed_across_pages(self):
        for n in range(30):
            self.sell(self.cashier_a, self.product(self.shop_a, f"Item {n:02}", f"IT{n}", "5.00"), 1)
        first = self.report(self.manager_a, sort='name')
        self.assertEqual(len(first.context['page_obj']), 25)
        self.assertEqual(first.context['page_obj'].paginator.count, 30)
        second = self.report(self.manager_a, sort='name', page=2)
        self.assertEqual([r['product__name'] for r in second.context['page_obj']],
                         [f"Item {n:02}" for n in range(25, 30)])
        # Totals cover all pages and agree with the revenue for the same dates.
        self.assertEqual(first.context['totals']['units'], 30)
        self.assertEqual(first.context['totals']['revenue'], D("150.00"))
        self.assertEqual(first.context['totals']['revenue'], first.context['revenue_today'])
        self.assertContains(second, 'page=1')

    def test_page_links_keep_the_filters(self):
        for n in range(26):
            self.sell(self.cashier_a, self.product(self.shop_a, f"Item {n:02}", f"IT{n}", "5.00"), 1)
        resp = self.report(self.manager_a, sort='units', q='item')
        self.assertContains(resp, 'sort=units')
        self.assertContains(resp, 'q=item')

    def test_profit_and_sale_count(self):
        self.sell(self.cashier_a, self.oil_a, 2)
        self.sell(self.cashier_a, self.oil_a, 1)
        row = self.report(self.manager_a).context['page_obj'][0]
        self.assertEqual((row['units'], row['sale_count']), (3, 2))
        self.assertEqual(row['revenue'], D("30.00"))
        self.assertEqual(row['profit'], D("12.00"))                     # 30 - 3 x 6

    def test_sorting(self):
        cheap = self.product(self.shop_a, "Cheap", "C", "1.00")
        self.sell(self.cashier_a, self.oil_a, 2)                       # revenue 20, 2 units
        self.sell(self.cashier_a, cheap, 9)                            # revenue 9, 9 units
        by_revenue = [r['product__name'] for r in self.report(self.manager_a).context['page_obj']]
        by_units = [r['product__name'] for r in self.report(self.manager_a, sort='units').context['page_obj']]
        self.assertEqual(by_revenue, ["Engine Oil", "Cheap"])
        self.assertEqual(by_units, ["Cheap", "Engine Oil"])
        self.report(self.manager_a, sort='DROP TABLE')                  # unknown sort: default, no crash

    def test_search_by_name_or_sku(self):
        filt = self.product(self.shop_a, "Oil Filter", "FLT-9", "20.00")
        self.sell(self.cashier_a, self.oil_a, 2)
        self.sell(self.cashier_a, filt, 1)
        self.assertEqual([r[0] for r in self.rows(self.report(self.manager_a, q='flt-9'))], ["Oil Filter"])
        self.assertEqual([r[0] for r in self.rows(self.report(self.manager_a, q='filtr'))], ["Oil Filter"])
        resp = self.report(self.manager_a, q='filter')
        self.assertEqual(resp.context['totals']['revenue'], D("20.00"))

    # ---- date windows --------------------------------------------------------
    def test_windows_include_today_and_have_the_stated_length(self):
        self.sell(self.cashier_a, self.oil_a, 1, days_ago=0)
        self.sell(self.cashier_a, self.oil_a, 1, days_ago=6)    # inside 7 days
        self.sell(self.cashier_a, self.oil_a, 1, days_ago=7)    # outside 7 days
        self.sell(self.cashier_a, self.oil_a, 1, days_ago=29)   # inside 30 days
        self.sell(self.cashier_a, self.oil_a, 1, days_ago=30)   # outside 30 days
        ctx = self.report(self.manager_a).context
        self.assertEqual(ctx['revenue_today'], D("10.00"))
        self.assertEqual(ctx['revenue_7d'], D("20.00"))
        self.assertEqual(ctx['revenue_30d'], D("40.00"))
        self.assertEqual(ctx['transactions_30d'], 4)
        # Products sold default to today; the presets cover the other windows.
        self.assertEqual(ctx['totals']['units'], 1)
        self.assertEqual(ctx['totals']['revenue'], ctx['revenue_today'])
        presets = {p['label']: p for p in ctx['presets']}
        self.assertTrue(presets['Today']['active'])
        for label, units in (('Yesterday', 0), ('Last 7 days', 2), ('Last 30 days', 4)):
            p = presets[label]
            got = self.report(self.manager_a, start_date=p['start'], end_date=p['end']).context
            self.assertEqual(got['totals']['units'], units, label)
            self.assertTrue(next(x for x in got['presets'] if x['label'] == label)['active'])

    def test_custom_period(self):
        self.sell(self.cashier_a, self.oil_a, 2, days_ago=40)
        self.sell(self.cashier_a, self.oil_a, 5)
        day = (timezone.localdate() - timedelta(days=40)).isoformat()
        resp = self.report(self.manager_a, start_date=day, end_date=day)
        self.assertEqual(self.rows(resp), [("Engine Oil", "Shop A", 2)])

    def test_bad_or_reversed_dates_do_not_crash(self):
        self.sell(self.cashier_a, self.oil_a, 2)
        resp = self.report(self.manager_a, start_date='31/12/2026', end_date='nope')
        self.assertEqual(resp.context['totals']['units'], 2)
        today = timezone.localdate()
        resp = self.report(self.manager_a, start_date=today.isoformat(),
                           end_date=(today - timedelta(days=3)).isoformat())
        self.assertEqual(resp.context['totals']['units'], 2)

    # ---- payment mix ---------------------------------------------------------
    def test_payment_mix_is_scoped_labelled_and_net_of_change(self):
        resolved, per = resolve_cart([{'id': self.oil_a.id, 'qty': 3}], self.shop_a)
        record_sale(location=self.shop_a, user=self.cashier_a, session=self.sessions[self.cashier_a.pk],
                    resolved=resolved, qty_per_product=per,
                    payments=[{'method': 'CASH', 'amount': '50'}])           # 20 change
        self.sell(self.cashier_b, self.oil_b, 1)
        mix = self.report(self.manager_a).context['payment_mix']
        self.assertEqual(mix, [{'method': 'Cash', 'total': D("30.00")}])

    def test_empty_report(self):
        resp = self.report(self.owner)
        self.assertContains(resp, 'No products sold in this period.')
        self.assertEqual(resp.context['totals']['units'], 0)

    def test_staff_performance_survives_bad_dates(self):
        self.client.force_login(self.manager_a)
        resp = self.client.get(reverse('dashboard:staff_performance'), {'start_date': 'x', 'end_date': '2026-13-45'})
        self.assertEqual(resp.status_code, 200)


class DailyReportTests(ReportTestBase):
    """The Daily Report: one day, end to end, and whether it tallies."""

    def day(self, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse('dashboard:daily_report'), params)
        self.assertEqual(resp.status_code, 200)
        return resp.context

    def sale(self, cashier, product, qty, payments, customer=None):
        resolved, per = resolve_cart([{'id': product.id, 'qty': qty}], cashier.assigned_location)
        return record_sale(location=cashier.assigned_location, user=cashier,
                           session=self.sessions[cashier.pk], resolved=resolved,
                           qty_per_product=per, payments=payments, customer=customer)

    def money(self, ctx, label):
        row = next(r for r in ctx['money_rows'] if r['label'] == label)
        return {m: v for m, v in row['by_method'].items() if v}

    def test_a_full_day_adds_up_and_tallies(self):
        ama = Customer.objects.create(phone_number="0241", first_name="Ama", location=self.shop_a)

        # Yesterday: a credit sale of 30, nothing paid.
        old = self.sale(self.cashier_a, self.oil_a, 3, [], customer=ama)
        Sale.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=1))

        # Today:
        self.sale(self.cashier_a, self.oil_a, 2, [{'method': 'CASH', 'amount': '50'}])        # 20, 30 change
        self.sale(self.cashier_a, self.oil_a, 1, [{'method': 'MOMO', 'amount': '10'}])        # 10
        credit = self.sale(self.cashier_a, self.oil_a, 4, [{'method': 'CASH', 'amount': '15'}],
                           customer=ama)                                                       # 40, owes 25
        record_settlement(sale=old, user=self.cashier_a, session=self.sessions[self.cashier_a.pk],
                          tendered=[('CASH', D("30"))])                                        # old debt paid
        refund_sale = self.sale(self.cashier_a, self.oil_a, 1, [{'method': 'CASH', 'amount': '10'}])
        record_refund(sale=refund_sale, user=self.manager_a, session=self.sessions[self.manager_a.pk],
                      item_ids=[i.id for i in refund_sale.items.all()], method='CASH', reason='x')

        ctx = self.day(self.manager_a)
        s = ctx['sales_summary']
        # The refunded sale still counts as a sale made today (status REFUNDED).
        self.assertEqual(s['count'], 4)
        self.assertEqual((s['gross'], s['refunded'], s['billed']), (D("80.00"), D("10.00"), D("70.00")))
        self.assertEqual((s['paid'], s['owed']), (D("45.00"), D("25.00")))
        self.assertEqual(s['units'], 7)
        self.assertEqual(s['paid'] + s['owed'], s['billed'])
        self.assertEqual([c.pk for c in ctx['credit_sales']], [credit.pk])

        self.assertEqual(self.money(ctx, "Taken on today's sales"), {'CASH': D("75.00"), 'MOMO': D("10.00")})
        self.assertEqual(self.money(ctx, 'Debt collected'), {'CASH': D("30.00")})
        self.assertEqual(self.money(ctx, 'Change given'), {'CASH': D("-30.00")})
        self.assertEqual(self.money(ctx, 'Refunds paid out'), {'CASH': D("-10.00")})
        self.assertEqual(ctx['money_net']['by_method']['CASH'], D("65.00"))
        self.assertEqual(ctx['money_net']['total'], D("75.00"))

        drawers = {d['session'].user_id: d for d in ctx['drawers']}
        self.assertEqual(drawers[self.cashier_a.pk]['session'].total_cash_sales, D("75.00"))  # 20+15+30+10
        self.assertEqual(drawers[self.cashier_a.pk]['expected'], D("75.00"))
        self.assertEqual(drawers[self.manager_a.pk]['expected'], D("-10.00"))   # refund paid from it
        # Cash in the drawers equals the net cash that moved today.
        self.assertEqual(sum(d['session'].total_cash_sales for d in ctx['drawers']),
                         ctx['money_net']['by_method']['CASH'])

        self.assertEqual(ctx['issues'], [])
        self.client.force_login(self.manager_a)
        self.assertContains(self.client.get(reverse('dashboard:daily_report')), 'Everything tallies')

    def test_a_discrepancy_is_shown(self):
        sale = self.sale(self.cashier_a, self.oil_a, 2, [{'method': 'CASH', 'amount': '20'}])
        Sale.objects.filter(pk=sale.pk).update(amount_paid=D("5.00"))
        ctx = self.day(self.manager_a)
        self.assertTrue(any(f.check == 'amount-paid' for f in ctx['issues']))
        self.client.force_login(self.manager_a)
        self.assertContains(self.client.get(reverse('dashboard:daily_report')), 'to check for this day')

    def test_a_shift_left_open_for_days_is_flagged(self):
        session = self.sessions[self.cashier_a.pk]
        RegisterSession.objects.filter(pk=session.pk).update(start_time=timezone.now() - timedelta(days=5))
        # Five days of cash in the drawer, only today's on the page: that is not
        # a mismatch, it is a shift that should have been closed.
        self.sale(self.cashier_a, self.oil_a, 2, [{'method': 'CASH', 'amount': '20'}])
        old = self.sale(self.cashier_a, self.oil_a, 1, [{'method': 'CASH', 'amount': '10'}])
        Sale.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
        SalePayment.objects.filter(sale=old).update(created_at=timezone.now() - timedelta(days=3))
        ctx = self.day(self.manager_a)
        self.assertTrue(any(d['multi_day'] for d in ctx['drawers']))
        self.assertEqual([f.check for f in ctx['issues']], ['long-shift'])

    def test_products_by_shop_not_by_name(self):
        self.sale(self.cashier_a, self.oil_a, 8, [{'method': 'CASH', 'amount': '80'}])
        self.sale(self.cashier_b, self.oil_b, 3, [{'method': 'CASH', 'amount': '36'}])
        rows = [(p['product__location__name'], p['units']) for p in self.day(self.owner)['products']]
        self.assertCountEqual(rows, [("Shop A", 8), ("Shop B", 3)])
        a_only = self.day(self.owner, location=self.shop_a.id)
        self.assertEqual([p['units'] for p in a_only['products']], [8])
        self.assertEqual(a_only['sales_summary']['billed'], D("80.00"))

    def test_other_days_and_bad_dates(self):
        yesterday = self.sale(self.cashier_a, self.oil_a, 2, [{'method': 'CASH', 'amount': '20'}])
        Sale.objects.filter(pk=yesterday.pk).update(created_at=timezone.now() - timedelta(days=1))
        SalePayment.objects.filter(sale=yesterday).update(created_at=timezone.now() - timedelta(days=1))
        self.sale(self.cashier_a, self.oil_a, 5, [{'method': 'CASH', 'amount': '50'}])

        self.assertEqual(self.day(self.manager_a)['sales_summary']['units'], 5)
        prev = self.day(self.manager_a, date=(timezone.localdate() - timedelta(days=1)).isoformat())
        self.assertEqual(prev['sales_summary']['units'], 2)
        self.assertEqual(prev['money_net']['by_method']['CASH'], D("20.00"))
        self.assertIsNotNone(prev['next_day'])
        self.assertEqual(self.day(self.manager_a, date='garbage')['day'], timezone.localdate())
        future = (timezone.localdate() + timedelta(days=3)).isoformat()
        self.assertEqual(self.day(self.manager_a, date=future)['day'], timezone.localdate())

    def test_manager_sees_only_their_shop(self):
        self.sale(self.cashier_b, self.oil_b, 3, [{'method': 'CASH', 'amount': '36'}])
        ctx = self.day(self.manager_a, location=self.shop_b.id)
        self.assertEqual(ctx['sales_summary']['count'], 0)
        self.assertEqual(ctx['money_net']['total'], D("0.00"))
        self.assertTrue(all(d['session'].location_id == self.shop_a.id for d in ctx['drawers']))

    def test_cashiers_cannot_open_it(self):
        self.client.force_login(self.cashier_a)
        self.assertEqual(self.client.get(reverse('dashboard:daily_report')).status_code, 302)

    def test_empty_day(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('dashboard:daily_report'))
        self.assertContains(resp, 'Nothing sold on this day.')
        self.assertContains(resp, 'Everything tallies')


class ReportsStartAndSummaryTests(ReportTestBase):
    """Starting the reports afresh from a date, and the period summary."""

    def backdate(self, sale, days):
        when = timezone.now() - timedelta(days=days)
        Sale.objects.filter(pk=sale.pk).update(created_at=when)
        SalePayment.objects.filter(sale=sale).update(created_at=when)

    def test_period_summary(self):
        from apps.customers.models import Customer
        ama = Customer.objects.create(phone_number="0241", first_name="Ama", location=self.shop_a)
        self.sell(self.cashier_a, self.oil_a, 3)                                     # 30 paid
        resolved, per = resolve_cart([{'id': self.oil_a.id, 'qty': 2}], self.shop_a)
        record_sale(location=self.shop_a, user=self.cashier_a, session=self.sessions[self.cashier_a.pk],
                    resolved=resolved, qty_per_product=per, customer=ama,
                    payments=[{'method': 'MOMO', 'amount': '5'}])                    # 20, owes 15
        s = self.report(self.manager_a).context['summary']
        self.assertEqual((s['revenue'], s['units'], s['sales']), (D("50.00"), 5, 2))
        self.assertEqual(s['profit'], D("20.00"))                                    # 50 - 5 x 6
        self.assertEqual(s['margin'], D("40"))
        self.assertEqual(s['average'], D("25.00"))
        self.assertEqual((s['collected'], s['owed']), (D("35.00"), D("15.00")))

    def test_day_by_day_for_longer_periods(self):
        self.backdate(self.sell(self.cashier_a, self.oil_a, 2), 2)
        self.sell(self.cashier_a, self.oil_a, 1)
        today = timezone.localdate()
        ctx = self.report(self.manager_a, start_date=(today - timedelta(days=3)).isoformat(),
                          end_date=today.isoformat()).context
        self.assertEqual([(d['day'], d['units']) for d in ctx['by_day']],
                         [(today - timedelta(days=n), u) for n, u in ((0, 1), (1, 0), (2, 2), (3, 0))])
        self.assertEqual(self.report(self.manager_a).context['by_day'], [])        # one day: not needed

    def test_nothing_before_the_start_date_counts(self):
        self.backdate(self.sell(self.cashier_a, self.oil_a, 5), 3)                 # the "messed up" days
        self.sell(self.cashier_a, self.oil_a, 2)
        today = timezone.localdate()
        with self.settings(REPORTS_START_DATE=today.isoformat()):
            ctx = self.report(self.manager_a, start_date=(today - timedelta(days=10)).isoformat()).context
            self.assertEqual(ctx['start_date'], today.isoformat())                    # clamped
            self.assertEqual(ctx['summary']['units'], 2)
            self.assertEqual((ctx['revenue_7d'], ctx['revenue_30d']), (D("20.00"), D("20.00")))
            self.assertEqual(ctx['transactions_30d'], 1)
            self.assertEqual([p['label'] for p in ctx['presets']], ['Today'])        # the rest are the same day
            self.assertEqual(ctx['reports_start'], today)
            self.assertContains(self.report(self.manager_a), 'Reports count sales from')
        # Without it, everything counts again — nothing was deleted.
        ctx = self.report(self.manager_a, start_date=(today - timedelta(days=10)).isoformat()).context
        self.assertEqual(ctx['summary']['units'], 7)

    def test_presets_after_a_start_date_a_few_days_ago(self):
        today = timezone.localdate()
        with self.settings(REPORTS_START_DATE=(today - timedelta(days=3)).isoformat()):
            presets = self.report(self.manager_a).context['presets']
        labels = [p['label'] for p in presets]
        self.assertEqual(labels[:3], ['Today', 'Yesterday', 'Last 7 days'])
        self.assertEqual(presets[2]['start'], (today - timedelta(days=3)).isoformat())
        self.assertNotIn('Last 30 days', labels)                                    # same days as "Last 7 days"

    def test_a_bad_start_date_setting_is_ignored(self):
        self.sell(self.cashier_a, self.oil_a, 1)
        with self.settings(REPORTS_START_DATE='soon'):
            self.assertEqual(self.report(self.manager_a).context['summary']['units'], 1)

    def test_daily_report_marks_days_before_the_start(self):
        today = timezone.localdate()
        self.client.force_login(self.manager_a)
        with self.settings(REPORTS_START_DATE=today.isoformat()):
            old = self.client.get(reverse('dashboard:daily_report'),
                                  {'date': (today - timedelta(days=1)).isoformat()})
            self.assertTrue(old.context['before_start'])
            self.assertContains(old, 'before the reports start date')
            self.assertFalse(self.client.get(reverse('dashboard:daily_report')).context['before_start'])
