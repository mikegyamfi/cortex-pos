"""
Where did the stock go? Every recorded movement of a product at a shop.

Answers "we had 11, sold 8, 3 are left — but reports show only 3 sold":
lists sales (with date, invoice and whether refunded), refund restocks,
stock adjustments, and transfers in and out — including stock pulled out of
this shop when another shop received goods "from" it, which is recorded
against the receiving shop's product.

    python manage.py product_history "engine oil"
    python manage.py product_history OIL-5W30 --location "Main Shop" --days 14

Changes made by editing a batch quantity in Django admin, or batches removed
by dedupe_stock_batches, leave no record and cannot be listed.
"""
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.core.search import search_queryset
from apps.inventory.models import StockAdjustment, StockBatch, StockTransferItem
from apps.location.models import Location
from apps.products.models import Product
from apps.sales.models import SaleItem


class Command(BaseCommand):
    help = "List every recorded stock movement for a product (sales, refunds, adjustments, transfers)."

    def add_arguments(self, parser):
        parser.add_argument('product', help="Product name or SKU (partial is fine).")
        parser.add_argument('--location', default=None, help="Only this shop (by name).")
        parser.add_argument('--days', type=int, default=None, help="Only the last N days of movements.")

    def handle(self, *args, **opts):
        products = Product.objects.select_related('location')
        if opts['location']:
            loc = Location.objects.filter(name__iexact=opts['location']).first()
            if loc is None:
                raise CommandError(f"No location named {opts['location']!r}.")
            products = products.filter(location=loc)
        products = list(search_queryset(products, opts['product'], ['name', 'sku', 'barcode'], fuzzy=False)[:10])
        if not products:
            raise CommandError(f"No product matches {opts['product']!r}.")

        since = timezone.now() - timedelta(days=opts['days']) if opts['days'] else None
        for product in products:
            self._one(product, since)

    def _one(self, product, since):
        shop = product.location
        out = self.stdout.write
        out(self.style.MIGRATE_HEADING(
            f"\n{product.name} [{product.sku}] @ {shop.name if shop else 'no shop'}"))

        batches = list(StockBatch.objects.filter(product=product).order_by('received_date'))
        on_hand = sum(b.quantity for b in batches)
        out(f"  On hand now: {on_hand}")
        for b in batches:
            out(f"    batch {b.id}: {b.quantity} left, received "
                f"{timezone.localtime(b.received_date):%Y-%m-%d %H:%M}{' (' + b.batch_number + ')' if b.batch_number else ''}")

        moves = []   # (when, kind, qty change for this shop, detail)

        for item in SaleItem.objects.filter(product=product).select_related('sale', 'sale__cashier'):
            sale = item.sale
            note = f"{sale.invoice_number} {sale.get_status_display()} by {sale.cashier or '?'}"
            if item.is_refunded:
                note += " - line REFUNDED (units came back)"
            moves.append((sale.created_at, 'SALE', -item.quantity, note))

        for adj in StockAdjustment.objects.filter(batch__product=product).select_related('performed_by'):
            kind = 'REFUND RESTOCK' if adj.reason == 'RETURN' else f'ADJUST {adj.get_reason_display()}'
            moves.append((adj.created_at, kind, adj.adjusted_quantity,
                          f"by {adj.performed_by or '?'}: {adj.notes[:80]}"))

        # Transfers. Items name ONE product, but each shop has its own copy, so
        # match this shop's side by SKU as well (receiving "from" a shop pulls
        # the source shop's copy while recording the receiving shop's product).
        same_item = Q(product=product) | Q(product__sku=product.sku)
        transfers = StockTransferItem.objects.filter(same_item).select_related(
            'transfer', 'transfer__source_location', 'transfer__destination_location')
        for t in transfers:
            tr = t.transfer
            if tr.source_location_id == product.location_id and t.quantity_sent:
                moves.append((tr.created_at, 'TRANSFER OUT', -t.quantity_sent,
                              f"{tr.reference_number} to {tr.destination_location.name} ({tr.get_status_display()})"))
            if tr.destination_location_id == product.location_id and t.quantity_received:
                moves.append((tr.created_at, 'TRANSFER IN', t.quantity_received,
                              f"{tr.reference_number} from {tr.source_location.name} ({tr.get_status_display()})"))

        if since is not None:
            moves = [m for m in moves if m[0] >= since]
        moves.sort(key=lambda m: m[0])

        out("  Movements:" if moves else "  No recorded movements.")
        totals = defaultdict(int)
        for when, kind, qty, note in moves:
            refunded = 'REFUNDED' in note
            out(f"    {timezone.localtime(when):%Y-%m-%d %H:%M}  {kind:<22} {qty:+5d}  {note}")
            if kind == 'SALE' and not refunded:
                totals['sold'] += -qty
            elif kind == 'SALE':
                totals['sold then refunded'] += -qty
            elif kind == 'REFUND RESTOCK':
                pass   # already shown on the refunded sale line
            else:
                totals[kind.lower()] += qty

        if moves:
            out("  Summary" + (" (selected days)" if since else "") + ":")
            for key, val in totals.items():
                out(f"    {key}: {val}")
            # Sales by day, so "sold 8" can be matched against a report for one day.
            per_day = defaultdict(int)
            for when, kind, qty, note in moves:
                if kind == 'SALE' and 'REFUNDED' not in note:
                    per_day[timezone.localtime(when).date()] += -qty
            if per_day:
                out("  Units sold by day: " + ", ".join(f"{d:%a %d %b} {n}" for d, n in sorted(per_day.items())))
        out("  Not listed: batch quantities edited in Django admin, and batches removed by dedupe_stock_batches.")
