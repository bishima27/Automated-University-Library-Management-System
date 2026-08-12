"""
circulation/management/commands/refresh_overdue_loans.py

Owner: Mbuala Wonder Ephraim Mbakadi

Run once a day (e.g. via a scheduled job on Render/Railway) to recompute
the `status` field on every active loan. This keeps `status` -- an
indexed field -- accurate for Emmanuel's reports and gives Samuel's
notifications module a cheap `Loan.objects.overdue()` (or
`status=STATUS_OVERDUE`) query to build overdue alerts from, instead of
recomputing `is_overdue` for every loan on every request.

Usage:
    python manage.py refresh_overdue_loans
"""
from django.core.management.base import BaseCommand

from circulation.models import Loan


class Command(BaseCommand):
    help = "Recompute overdue status for all active loans."

    def handle(self, *args, **options):
        active_loans = Loan.objects.active()
        total = active_loans.count()
        updated = 0

        for loan in active_loans.iterator():
            previous_status = loan.status
            loan.refresh_status()
            if loan.status != previous_status:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Checked {total} active loan(s), updated {updated} status change(s)."
            )
        )
