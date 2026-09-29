"""
Delete accidental duplicate stock receipts, keeping the oldest batch.

Background: receiving used to be a multi-step flow that often did not complete,
so staff repeated it and each attempt created its own StockBatch. That stock
never physically arrived twice, so the extra batches are phantom and the counts
are too high.

This command KEEPS the oldest batch of a product at a location and DELETES the
rest, which lowers the stock on hand. Sale and adjustment history pointing at a
deleted batch is repointed to the batch that is kept, so the audit trail
survives (StockBatch is PROTECTed by both).

It is a dry run unless --commit is passed. Always read the dry run first.

    # See what would go, narrowed to accidental same-day repeats
    python manage.py dedupe_stock_batches --same-day
    # Apply it
    python manage.py dedupe_stock_batches --same-day --commit

Use --merge instead of deleting when the repeats were real deliveries whose
quantities should be added together.
"""
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import StockAdjustment, StockBatch
from apps.location.models import Location
from apps.sales.models import SaleItem


class Command(BaseCommand):
    help = ("Delete duplicate stock batches of the same product, keeping the oldest "
            "(lowers stock on hand). Dry run unless --commit is passed.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help="Actually write the changes. Without this the command only reports.",
        )
        parser.add_argument(
            '--same-day', action='store_true',
            help="Only treat batches received on the SAME DAY as duplicates of each other. "
                 "This targets the repeated-receiving mistake and leaves genuine "
                 "restocks from other days alone. Recommended.",
        )
        parser.add_argument(
            '--location', type=str, default=None,
            help="Limit to one shop/warehouse by name (default: every location).",
        )
        parser.add_argument(
            '--sku', type=str, default=None,
            help="Limit to one product SKU.",
        )
        parser.add_argument(
            '--since', type=str, default=None,
            help="Only consider batches received on or after this date (YYYY-MM-DD), "
                 "e.g. the day the broken flow went live.",
        )
        parser.add_argument(
            '--until', type=str, default=None,
            help="Only consider batches received on or before this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            '--merge', action='store_true',
            help="Add the duplicates' quantities into the kept batch instead of dropping "
                 "them. Use when the repeats were real deliveries.",
        )

    def _parse_date(self, value, label):
        if not value:
            return None
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            raise CommandError(f"--{label} must be a date like 2026-09-01, got {value!r}")

    def handle(self, *args, **options):
        commit = options['commit']
        merge = options['merge']
        since = self._parse_date(options['since'], 'since')
        until = self._parse_date(options['until'], 'until')

        batches = StockBatch.objects.select_related('product', 'location')

        if options['location']:
            location = Location.objects.filter(name__iexact=options['location']).first()
            if not location:
                known = ', '.join(Location.objects.values_list('name', flat=True)) or 'none'
                raise CommandError(f"No location named {options['location']!r}. Known: {known}")
            batches = batches.filter(location=location)

        if options['sku']:
            batches = batches.filter(product__sku__iexact=options['sku'])
            if not batches.exists():
                raise CommandError(f"No stock batches for SKU {options['sku']!r}.")

        if since:
            batches = batches.filter(
                received_date__gte=timezone.make_aware(datetime.combine(since, time.min))
            )
        if until:
            batches = batches.filter(
                received_date__lte=timezone.make_aware(datetime.combine(until, time.max))
            )

        # Group the rows that count as duplicates of one another.
        groups = {}
        for batch in batches.order_by('received_date', 'id'):
            key = (batch.product_id, batch.location_id)
            if options['same_day']:
                key += (timezone.localtime(batch.received_date).date(),)
            groups.setdefault(key, []).append(batch)

        duplicates = [rows for rows in groups.values() if len(rows) > 1]

        if not duplicates:
            self.stdout.write(self.style.SUCCESS(
                "Nothing to do - no product has duplicate batches in that range."
            ))
            return

        verb = "merge" if merge else "delete"
        groups_touched = rows_touched = units_changed = 0
        repointed_items = repointed_adjustments = 0

        with transaction.atomic():
            for rows in duplicates:
                keeper, extras = rows[0], rows[1:]
                extra_ids = [b.id for b in extras]
                extra_units = sum(b.quantity for b in extras)
                before = keeper.quantity + extra_units

                if merge:
                    after = before
                    value = sum(b.quantity * b.cost_price for b in rows)
                    new_cost = ((value / before).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                                if before else keeper.cost_price)
                    expiries = [b.expiry_date for b in rows if b.expiry_date]
                    new_expiry = min(expiries) if expiries else None
                else:
                    after = keeper.quantity
                    new_cost, new_expiry = keeper.cost_price, keeper.expiry_date

                self.stdout.write(
                    f"{keeper.product.name} ({keeper.product.sku}) @ {keeper.location.name}: "
                    f"{len(rows)} batches [{', '.join(str(b.quantity) for b in rows)}] "
                    f"-> keep #{keeper.id} ({keeper.quantity}), {verb} {len(extras)} "
                    f"({extra_units} units) | on hand {before} -> {after}"
                )
                if not merge and keeper.quantity == 0:
                    self.stdout.write(self.style.WARNING(
                        f"  ! the kept batch is empty, so {keeper.product.name} will read 0 "
                        f"at {keeper.location.name}. Check this one before committing."
                    ))

                if commit:
                    # Keep history attached to the surviving batch (both FKs are PROTECT).
                    repointed_items += SaleItem.objects.filter(
                        source_batch_id__in=extra_ids).update(source_batch=keeper)
                    repointed_adjustments += StockAdjustment.objects.filter(
                        batch_id__in=extra_ids).update(batch=keeper)

                    if merge:
                        keeper.quantity = after
                        keeper.cost_price = new_cost
                        keeper.expiry_date = new_expiry
                        keeper.save(update_fields=['quantity', 'cost_price', 'expiry_date', 'updated_at'])

                    StockBatch.objects.filter(id__in=extra_ids).delete()

                groups_touched += 1
                rows_touched += len(extras)
                units_changed += 0 if merge else extra_units

            if not commit:
                transaction.set_rollback(True)

        summary = f"\n{groups_touched} product/location group(s), {rows_touched} batch row(s)"
        if merge:
            summary += " folded into the oldest batch (stock unchanged)."
        else:
            summary += f" deleted, removing {units_changed} phantom unit(s) from stock."

        if commit:
            self.stdout.write(self.style.SUCCESS(
                summary + f"\nRepointed {repointed_items} sale item(s) and "
                          f"{repointed_adjustments} adjustment(s). Done."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                summary + "\nDRY RUN — nothing was written. Re-run with --commit to apply."
            ))
