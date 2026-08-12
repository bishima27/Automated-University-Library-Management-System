"""
circulation/models.py

Owner: Mbuala Wonder Ephraim Mbakadi (Team C -- Circulation module: logic)

Defines Loan and Fine, and the state transitions (borrow / renew / return)
that the rest of the Circulation module (Leona's student-facing views,
Frederick's admin dashboard) call into. Due-date and fine calculation are
implemented as pure functions in `business_rules.py` so Team E can
unit-test them without a database, and so Team C (Emmanuel's reports,
Samuel's notifications) can reuse the exact same logic instead of
duplicating it.

Cross-module dependencies (per Team C's shared conventions, set by Maxwell):
  - catalog.models.BookCopy    (Cecil)  -- one row per physical copy; must
                                            expose a `status` field with at
                                            least the values "AVAILABLE" and
                                            "BORROWED" (see business_rules
                                            .COPY_STATUS_AVAILABLE / _ON_LOAN)
  - settings.AUTH_USER_MODEL   (Pius)   -- the borrower

v2 changes (per Mbuala's rewrite):
  - Loan now points at BookCopy (a specific physical copy) instead of Book,
    so two students can never be issued "the same" copy.
  - member -> borrower, borrowed_at -> issue_date, returned_at -> return_date,
    matching the naming Ekuman is using in the admin dashboard.
  - fine_amount / fine_paid have moved out of Loan and into their own Fine
    model (is_paid, not fine_paid), so a loan's fine has its own audit
    trail (assessed_at / paid_at) instead of being two bare columns on Loan.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from . import business_rules as rules


class LoanQuerySet(models.QuerySet):
    """Common filters reused by Leona's views, Frederick's dashboard,
    Emmanuel's reports and Samuel's notifications, so nobody has to
    re-derive 'what counts as overdue' by hand."""

    def active(self):
        """Loans currently checked out (not yet returned)."""
        return self.filter(return_date__isnull=True)

    def returned(self):
        return self.filter(return_date__isnull=False)

    def overdue(self):
        """Active loans whose due date has already passed."""
        return self.active().filter(due_date__lt=timezone.localdate())

    def for_borrower(self, borrower):
        return self.filter(borrower=borrower)


class Loan(models.Model):
    STATUS_ACTIVE = "ACTIVE"
    STATUS_OVERDUE = "OVERDUE"
    STATUS_RETURNED = "RETURNED"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_OVERDUE, "Overdue"),
        (STATUS_RETURNED, "Returned"),
    ]

    book_copy = models.ForeignKey(
        "catalog.BookCopy", on_delete=models.PROTECT, related_name="loans"
    )
    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="loans"
    )
    issue_date = models.DateTimeField(default=timezone.now)
    due_date = models.DateField(blank=True)
    return_date = models.DateTimeField(null=True, blank=True)
    renewal_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE
    )

    objects = LoanQuerySet.as_manager()

    class Meta:
        ordering = ["-issue_date"]
        indexes = [
            models.Index(fields=["due_date"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.book_copy} -> {self.borrower} (due {self.due_date})"

    def save(self, *args, **kwargs):
        # Auto-fill the due date on first save so callers never have to
        # compute it themselves -- one less place for the rule to drift.
        if not self.due_date:
            self.due_date = rules.calculate_due_date(self.issue_date, self.renewal_count)
        super().save(*args, **kwargs)

    # ---- derived state ------------------------------------------------
    @property
    def is_returned(self) -> bool:
        return self.return_date is not None

    @property
    def is_overdue(self) -> bool:
        if self.is_returned:
            return False
        return timezone.localdate() > self.due_date

    @property
    def days_overdue(self) -> int:
        return rules.days_overdue(self)

    @property
    def current_fine(self) -> Decimal:
        """Fine as of right now (or frozen as of the return date, if already returned).
        This is always computed live from the dates -- it is NOT the same object
        as the persisted Fine row, which is only written on return (see
        `mark_returned`) or by an explicit waiver/adjustment elsewhere."""
        return rules.calculate_fine(self)

    def get_fine(self) -> "Fine | None":
        """Safe accessor for the related Fine row (None if not yet assessed)."""
        return getattr(self, "fine", None)

    # ---- business operations -------------------------------------------
    def renew(self) -> None:
        """Extend the due date by one renewal period, enforcing eligibility rules."""
        eligible, reason = rules.can_renew(self)
        if not eligible:
            raise ValidationError(reason)
        self.renewal_count += 1
        self.due_date = rules.calculate_due_date(self.issue_date, self.renewal_count)
        self.status = self.STATUS_ACTIVE
        self.save(update_fields=["renewal_count", "due_date", "status"])

    @transaction.atomic
    def mark_returned(self) -> None:
        """Close out the loan: assess/lock in the fine and free up the copy."""
        if self.is_returned:
            raise ValidationError("This loan has already been returned.")

        self.return_date = timezone.now()
        self.status = self.STATUS_RETURNED
        self.save(update_fields=["return_date", "status"])

        fine_amount = rules.calculate_fine(self)
        if fine_amount > 0:
            Fine.objects.update_or_create(
                loan=self, defaults={"amount": fine_amount}
            )

        # Conditional update rather than blind assignment, in case the copy
        # was already flagged LOST/DAMAGED by a librarian while out on loan.
        self.book_copy.__class__.objects.filter(
            pk=self.book_copy_id, status=rules.COPY_STATUS_ON_LOAN
        ).update(status=rules.COPY_STATUS_AVAILABLE)

    def refresh_status(self) -> None:
        """Recompute `status` from current dates; used by the nightly overdue job
        (see management/commands/refresh_overdue_loans.py) so Reports and
        Notifications can query a fast, indexed field instead of recomputing
        `is_overdue` for every loan on every page load."""
        if self.is_returned:
            self.status = self.STATUS_RETURNED
        elif self.is_overdue:
            self.status = self.STATUS_OVERDUE
        else:
            self.status = self.STATUS_ACTIVE
        self.save(update_fields=["status"])

    @classmethod
    @transaction.atomic
    def start_loan(cls, *, book_copy, borrower) -> "Loan":
        """
        Factory method that enforces the borrowing gate (can_borrow) before
        creating a Loan, and flips the specific BookCopy to ON_LOAN.
        Intended to be called from Leona's 'borrow' view, after the view
        has already picked *which* available copy of the book to issue.
        """
        active_count = cls.objects.active().for_borrower(borrower).count()
        outstanding_fines = Fine.objects.filter(
            loan__borrower=borrower, is_paid=False
        ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")

        eligible, reason = rules.can_borrow(
            active_loan_count=active_count,
            outstanding_fines=outstanding_fines,
            copy_is_available=(book_copy.status == rules.COPY_STATUS_AVAILABLE),
        )
        if not eligible:
            raise ValidationError(reason)

        # Compare-and-swap: only succeeds if the copy is still AVAILABLE at
        # the moment we write, so two simultaneous borrow requests for the
        # same copy can't both succeed.
        rows_updated = book_copy.__class__.objects.filter(
            pk=book_copy.pk, status=rules.COPY_STATUS_AVAILABLE
        ).update(status=rules.COPY_STATUS_ON_LOAN)
        if rows_updated == 0:
            raise ValidationError("This copy was just issued to someone else.")

        return cls.objects.create(book_copy=book_copy, borrower=borrower)


class Fine(models.Model):
    """
    A fine assessed against a Loan. Split out from Loan (per Ekuman's
    naming: `is_paid`, not `fine_paid`) so that fine bookkeeping --
    when it was assessed, when/whether it was paid -- has its own record
    instead of being two bare columns bolted onto Loan.
    """
    loan = models.OneToOneField(
        Loan, on_delete=models.CASCADE, related_name="fine"
    )
    amount = models.DecimalField(max_digits=6, decimal_places=2)
    is_paid = models.BooleanField(default=False)
    assessed_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-assessed_at"]
        indexes = [models.Index(fields=["is_paid"])]

    def __str__(self) -> str:
        paid = "paid" if self.is_paid else "unpaid"
        return f"Fine GHS {self.amount} ({paid}) -- {self.loan}"

    def mark_paid(self) -> None:
        if self.is_paid:
            raise ValidationError("This fine has already been paid.")
        self.is_paid = True
        self.paid_at = timezone.now()
        self.save(update_fields=["is_paid", "paid_at"])
