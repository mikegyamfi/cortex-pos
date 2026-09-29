"""
Prefix product names with a brand word (default "K2 ").

Products whose name already starts with the prefix are left alone.
Dry run unless --commit is passed. Slugs and SKUs are not touched.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.location.models import Location
from apps.products.models import Product


class Command(BaseCommand):
    help = 'Add "K2 " to the front of product names that do not already start with it.'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true',
                            help="Actually rename. Without this the command only reports.")
        parser.add_argument('--prefix', default='K2',
                            help='Prefix word to add (default: K2).')
        parser.add_argument('--location', default=None,
                            help="Limit to one shop/warehouse by name.")

    def handle(self, *args, **options):
        prefix = options['prefix'].strip()
        commit = options['commit']

        products = Product.objects.all().order_by('name')
        if options['location']:
            location = Location.objects.filter(name__iexact=options['location']).first()
            if not location:
                known = ', '.join(Location.objects.values_list('name', flat=True)) or 'none'
                raise CommandError(f"No location named {options['location']!r}. Known: {known}")
            products = products.filter(location=location)

        renamed = skipped = 0
        with transaction.atomic():
            for product in products:
                name = product.name.strip()
                if name.casefold().startswith(f"{prefix.casefold()} "):
                    skipped += 1
                    continue

                new_name = f"{prefix} {name}"
                self.stdout.write(f"{product.sku}: {product.name!r} -> {new_name!r}")
                if commit:
                    product.name = new_name
                    product.save(update_fields=['name', 'updated_at'])
                renamed += 1

            if not commit:
                transaction.set_rollback(True)

        summary = f"\n{renamed} renamed, {skipped} already prefixed."
        if commit:
            self.stdout.write(self.style.SUCCESS(summary + " Done."))
        else:
            self.stdout.write(self.style.WARNING(
                summary + "\nDRY RUN - nothing was written. Re-run with --commit to apply."
            ))
