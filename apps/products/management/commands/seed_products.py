"""
Seed the product catalogue from a CSV or plain-text file.

Every product is created with ZERO stock (no StockBatch) and ZERO prices
(cost / selling / wholesale all 0.00) — ready for staff to price and receive
stock afterwards. The command is idempotent (safe to re-run; existing products
are skipped), so it can be used both locally and in production.

Usage
-----
    # CSV with a header row (recognised columns: name, sku, barcode, category, unit)
    python manage.py seed_products --file products.csv

    # Plain text — one product name per line
    python manage.py seed_products --file names.txt

    # Put everything under a category (created if missing)
    python manage.py seed_products --file products.csv --category "General"

    # Preview without writing anything
    python manage.py seed_products --file products.csv --dry-run

Only `name` is required. SKUs are auto-generated from the name when not given,
and de-duplicated so they stay unique.
"""
import csv
import os
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from apps.location.models import Location
from apps.products.models import Product, Category, Unit


# Column aliases -> canonical field name (matched case-insensitively).
COLUMN_ALIASES = {
    'name': 'name', 'product': 'name', 'product name': 'name', 'description': 'name',
    'sku': 'sku', 'code': 'sku', 'item code': 'sku',
    'barcode': 'barcode', 'ean': 'barcode', 'upc': 'barcode',
    'category': 'category', 'group': 'category',
    'unit': 'unit', 'uom': 'unit',
    'location': 'location', 'shop': 'location', 'store': 'location',
}


class Command(BaseCommand):
    help = "Seed products from a CSV/TXT file with zero stock and zero prices."

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help="Path to a .csv/.txt/.json file of products.")
        parser.add_argument('--category', default=None,
                            help="Optional category name to assign to every seeded product (created if missing).")
        parser.add_argument('--location', '--shop', dest='location', default=None,
                            help="Bind every seeded product to this shop/location (must already exist).")
        parser.add_argument('--dry-run', action='store_true', help="Show what would happen without writing to the DB.")

    def handle(self, *args, **options):
        path = options['file']
        if not os.path.exists(path):
            raise CommandError(f"File not found: {path}")

        rows = self._read_rows(path)
        if not rows:
            raise CommandError("No product rows found in the file.")

        dry_run = options['dry_run']
        default_category_name = options['category']
        default_location_name = options['location']

        created = skipped = 0
        # Track SKUs already taken, keyed by (location_id, sku), so we don't
        # collide within the same shop before rows are written. SKUs are unique
        # PER SHOP, so the same SKU is fine in different shops.
        used_skus = {(p['location_id'], p['sku']) for p in Product.objects.values('location_id', 'sku')}

        with transaction.atomic():
            category = None
            if default_category_name:
                category = self._get_category(default_category_name, dry_run)

            shop = self._get_location(default_location_name) if default_location_name else None

            for row in rows:
                name = (row.get('name') or '').strip()
                if not name:
                    continue

                row_category = category
                if row.get('category'):
                    row_category = self._get_category(str(row['category']).strip(), dry_run)

                row_location = shop
                if row.get('location'):
                    row_location = self._get_location(str(row['location']).strip())
                loc_id = row_location.id if row_location else None

                # Idempotent, scoped to the shop: when an explicit SKU is given,
                # dedupe on (shop, SKU); otherwise dedupe on (shop, name).
                explicit_sku = (row.get('sku') or '').strip()
                if explicit_sku:
                    if Product.objects.filter(location=row_location, sku__iexact=explicit_sku).exists():
                        skipped += 1
                        continue
                    sku = explicit_sku
                else:
                    if Product.objects.filter(location=row_location, name__iexact=name).exists():
                        skipped += 1
                        continue
                    sku = self._unique_sku(name, used_skus, loc_id, row_location)
                used_skus.add((loc_id, sku))

                barcode = (row.get('barcode') or '').strip() or None
                if barcode and Product.objects.filter(location=row_location, barcode=barcode).exists():
                    barcode = None  # don't fail the whole run on a dup barcode in this shop

                if dry_run:
                    where = f" -> {row_location.name}" if row_location else ""
                    self.stdout.write(f"  + {name}  (sku={sku}){where}")
                    created += 1
                    continue

                Product.objects.create(
                    name=name,
                    sku=sku,
                    barcode=barcode,
                    location=row_location,
                    category=row_category,
                    unit=self._get_unit(row.get('unit'), dry_run),
                    cost_price=0,
                    selling_price=0,
                    wholesale_price=0,
                    tax_rate=0,
                )
                created += 1

            if dry_run:
                # Roll back anything (e.g. categories created) during a dry run.
                transaction.set_rollback(True)

        verb = "Would create" if dry_run else "Created"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {created} product(s); skipped {skipped} existing. "
            f"All seeded with 0 stock and 0 prices."
        ))

    # --- helpers ---------------------------------------------------------

    def _read_rows(self, path):
        """Return a list of dict rows with at least a 'name' key."""
        lower = path.lower()

        # JSON: an array of names, or an array of objects (name/sku/barcode/...).
        if lower.endswith('.json'):
            import json
            with open(path, encoding='utf-8-sig', errors='replace') as fh:
                data = json.load(fh)
            rows = []
            for entry in data:
                if isinstance(entry, str):
                    rows.append({'name': entry})
                elif isinstance(entry, dict):
                    rows.append({
                        COLUMN_ALIASES.get((k or '').strip().lower(), (k or '').strip().lower()): v
                        for k, v in entry.items()
                    })
            return rows

        is_csv = lower.endswith('.csv')
        with open(path, newline='', encoding='utf-8-sig', errors='replace') as fh:
            if is_csv:
                sample = fh.read(2048)
                fh.seek(0)
                has_header = bool(re.search(r'name|product|sku', sample, re.IGNORECASE))
                if has_header:
                    reader = csv.DictReader(fh)
                    rows = []
                    for raw in reader:
                        rows.append({
                            COLUMN_ALIASES.get((k or '').strip().lower(), (k or '').strip().lower()): (v or '')
                            for k, v in raw.items()
                        })
                    return rows
                # Headerless CSV: treat the first column as the product name.
                return [{'name': (cols[0] if cols else '')} for cols in csv.reader(fh)]
            # Plain text: one name per line.
            return [{'name': line.strip()} for line in fh if line.strip()]

    def _unique_sku(self, name, used, loc_id, location):
        """Generate a SKU unique within the given shop (location)."""
        base = re.sub(r'[^A-Z0-9]', '', slugify(name).upper()) or 'PROD'
        base = base[:40]
        sku = base
        n = 1
        while (loc_id, sku) in used or Product.objects.filter(location=location, sku=sku).exists():
            suffix = str(n)
            sku = base[:40 - len(suffix)] + suffix
            n += 1
        return sku

    def _get_category(self, name, dry_run):
        if dry_run:
            return Category.objects.filter(name__iexact=name).first()
        cat, _ = Category.objects.get_or_create(
            name=name, defaults={'slug': slugify(name) or 'category'}
        )
        return cat

    def _get_location(self, name):
        name = (name or '').strip()
        if not name:
            return None
        loc = Location.objects.filter(name__iexact=name).first()
        if not loc:
            raise CommandError(
                f"Shop/location '{name}' not found. Create it first "
                f"(Administration -> Warehouses & Shops) or check the spelling."
            )
        return loc

    def _get_unit(self, name, dry_run):
        name = (name or '').strip()
        if not name or dry_run:
            return None
        unit = Unit.objects.filter(name__iexact=name).first()
        if unit:
            return unit
        return Unit.objects.create(name=name, symbol=name[:10])
