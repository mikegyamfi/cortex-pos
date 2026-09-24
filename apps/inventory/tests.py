from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.inventory.models import StockBatch, StockTransfer
from apps.location.models import Location
from apps.products.models import Product

User = get_user_model()


class ReceiveStockFlowTests(TestCase):
    def setUp(self):
        self.warehouse = Location.objects.create(name="Main Warehouse", location_type='WAREHOUSE', address="x")
        self.shop = Location.objects.create(name="Test Shop", location_type='SHOP', address="y")
        self.manager = User.objects.create_user(
            username='mgr', password='pw', role='MANAGER', assigned_location=self.shop
        )
        self.shop_product = Product.objects.create(
            name="Bolt", slug="bolt-shop", sku="BLT-1", location=self.shop,
            cost_price=Decimal('10'), selling_price=Decimal('15'),
        )
        self.wh_product = Product.objects.create(
            name="Bolt", slug="bolt-wh", sku="BLT-1", location=self.warehouse,
            cost_price=Decimal('9'), selling_price=Decimal('15'),
        )
        StockBatch.objects.create(product=self.wh_product, location=self.warehouse,
                                  quantity=50, cost_price=Decimal('9'))
        self.client.force_login(self.manager)

    def _shop_qty(self):
        return sum(b.quantity for b in StockBatch.objects.filter(product=self.shop_product, location=self.shop))

    def _wh_qty(self):
        return sum(b.quantity for b in StockBatch.objects.filter(location=self.warehouse))

    def test_receive_without_source_just_adds_stock(self):
        res = self.client.post(reverse('inventory:receive_stock'), {
            'product': self.shop_product.id, 'quantity': 12,
            'cost_price': '', 'supplier': '', 'batch_number': '',
            'expiry_date': '', 'manufactured_date': '', 'source_location': '',
        })
        self.assertRedirects(res, reverse('inventory:dashboard'))
        self.assertEqual(self._shop_qty(), 12)
        self.assertEqual(self._wh_qty(), 50)
        batch = StockBatch.objects.get(product=self.shop_product)
        self.assertEqual(batch.cost_price, Decimal('10'))  # fell back to product cost
        self.assertFalse(StockTransfer.objects.exists())

    def test_receive_from_warehouse_deducts_there_and_logs_transfer(self):
        res = self.client.post(reverse('inventory:receive_stock'), {
            'product': self.shop_product.id, 'quantity': 20,
            'cost_price': '', 'supplier': '', 'batch_number': '',
            'expiry_date': '', 'manufactured_date': '',
            'source_location': self.warehouse.id,
        })
        self.assertRedirects(res, reverse('inventory:dashboard'))
        self.assertEqual(self._shop_qty(), 20)
        self.assertEqual(self._wh_qty(), 30)  # matched by SKU at the warehouse
        t = StockTransfer.objects.get()
        self.assertEqual(t.status, StockTransfer.Status.RECEIVED)
        self.assertEqual(t.source_location, self.warehouse)
        self.assertEqual(t.destination_location, self.shop)
        self.assertEqual(t.items.get().quantity_sent, 20)

    def test_receive_more_than_source_has_warns_but_completes(self):
        res = self.client.post(reverse('inventory:receive_stock'), {
            'product': self.shop_product.id, 'quantity': 60,
            'cost_price': '', 'supplier': '', 'batch_number': '',
            'expiry_date': '', 'manufactured_date': '',
            'source_location': self.warehouse.id,
        }, follow=True)
        self.assertEqual(self._shop_qty(), 60)
        self.assertEqual(self._wh_qty(), 0)
        msgs = [m.message for m in res.context['messages']]
        self.assertTrue(any('only had 50' in m for m in msgs), msgs)

    def test_zero_quantity_rejected(self):
        res = self.client.post(reverse('inventory:receive_stock'), {
            'product': self.shop_product.id, 'quantity': 0,
            'cost_price': '', 'supplier': '', 'batch_number': '',
            'expiry_date': '', 'manufactured_date': '', 'source_location': '',
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._shop_qty(), 0)

    def test_staff_only_sees_own_shop_products_and_other_locations_as_source(self):
        res = self.client.get(reverse('inventory:receive_stock'))
        form = res.context['form']
        self.assertNotIn('location', form.fields)
        self.assertEqual(list(form.fields['product'].queryset), [self.shop_product])
        self.assertEqual(list(form.fields['source_location'].queryset), [self.warehouse])

    def test_pending_transfer_can_be_received_in_one_step(self):
        transfer = StockTransfer.objects.create(
            source_location=self.warehouse, destination_location=self.shop,
            status=StockTransfer.Status.PENDING_APPROVAL, requested_by=self.manager,
        )
        item = transfer.items.create(product=self.shop_product, quantity_requested=15)
        res = self.client.post(reverse('inventory:receive_transfer', args=[transfer.pk]),
                               {f'received_qty_{item.id}': 15, 'notes': 'picked up myself'})
        self.assertRedirects(res, reverse('inventory:transfer_list'))
        transfer.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(transfer.status, StockTransfer.Status.RECEIVED)
        self.assertEqual(item.quantity_sent, 15)
        self.assertEqual(self._shop_qty(), 15)
        self.assertEqual(self._wh_qty(), 35)
