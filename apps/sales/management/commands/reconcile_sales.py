"""
Check that every sale, payment, drawer and customer balance tallies — and
repair what can be safely recomputed.

Background: before the money paths were hardened, several things could leave
stored totals out of step with what was actually recorded — double-submitted
checkouts, two requests updating the same drawer at once, refunds paying out
cash on goods that were never paid for, and payments that did not record which
drawer they went through. See apps/sales/reconcile.py for each check.

It is a dry run unless --commit is passed. Always read the dry run first.

    # Report everything
    python manage.py reconcile_sales
    # One shop, recent trading only
    python manage.py reconcile_sales --location "Main Shop" --since 2026-09-01
    # Apply the safe repairs (sales, open drawers, customers, payment drawers)
    python manage.py reconcile_sales --commit
    # ...and also correct expected cash / status on CLOSED shifts
    python manage.py reconcile_sales --commit --include-closed

Items marked REVIEW are never changed: they need someone to decide what
physically happened (e.g. refund a double-submitted sale from its page).
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.location.models import Location
from apps.sales.reconcile import reconcile


class Command(BaseCommand):
    help = ("Check sales, payments, drawers and customer balances tally; repair what "
            "is safe. Dry run unless --commit is passed.")

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true',
                            help="Actually write the repairs. Without this the command only reports.")
        parser.add_argument('--include-closed', action='store_true',
                            help="Also correct expected cash and status on CLOSED shifts "
                                 "(changes the shift history managers have already seen).")
        parser.add_argument('--location', type=str, default=None,
                            help="Limit to one shop/warehouse by name (default: every location).")
        parser.add_argument('--since', type=str, default=None,
                            help="Only sales/shifts from this date on (YYYY-MM-DD).")

    def handle(self, *args, **opts):
        location = None
        if opts['location']:
            try:
                location = Location.objects.get(name=opts['location'])
            except Location.DoesNotExist:
                raise CommandError(f"No location named {opts['location']!r}.")
        since = None
        if opts['since']:
            try:
                since = date.fromisoformat(opts['since'])
            except ValueError:
                raise CommandError("--since must be YYYY-MM-DD.")

        commit = opts['commit']
        with transaction.atomic():
            report = reconcile(fix=commit, include_closed=opts['include_closed'],
                               location=location, since=since)

        if not report.findings:
            self.stdout.write(self.style.SUCCESS("Everything tallies. Nothing to do."))
            return

        by_check = {}
        for f in report.findings:
            by_check.setdefault(f.check, []).append(f)

        for check, findings in by_check.items():
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{check} ({len(findings)})"))
            for f in findings:
                if f.fixed:
                    tag = self.style.SUCCESS('FIXED ')
                elif f.fixable:
                    tag = self.style.WARNING('FIX   ')
                else:
                    tag = self.style.ERROR('REVIEW')
                self.stdout.write(f"  {tag} {f.subject}: {f.detail}")

        fixed = sum(1 for f in report.findings if f.fixed)
        fixable = len(report.fixable) - fixed
        review = len(report.review)
        self.stdout.write('')
        if commit:
            self.stdout.write(self.style.SUCCESS(f"Repaired {fixed}."))
        elif fixable:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN: {fixable} can be repaired. Re-run with --commit to apply."))
        if review:
            self.stdout.write(self.style.ERROR(
                f"{review} need a person to review (marked REVIEW); these are never changed automatically."))
