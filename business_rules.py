"""
circulation/business_rules.py

Owner: Mbuala Wonder Ephraim Mbakadi (Team C -- Circulation module: logic)

Pure, model-light business rules for due dates, renewals and fines.
Kept separate from models.py so Team E (Gabriel) can unit test these
functions directly against fake/mock objects, and so the rest of Team C
(Emmanuel's reports, Samuel's notifications) can import the same source
of truth instead of re-implementing the maths in multiple places.

Every function here is deterministic given its inputs -- no database
queries happen inside this module except where explicitly noted.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Tuple

from django.utils import timezone

# ---- configurable constants (single source of truth) ----------------------
# Changing a business rule (e.g. loan length, fine rate) should only ever
# require editing values here -- nothing else in the codebase should
# hard-code these numbers.
LOAN_PERIOD_DAYS = 14              # standard borrowing period
RENEWAL_PERIOD_DAYS = 14           # each renewal extends the due date by this many days
MAX_RENEWALS = 2                   # a loan can be renewed at most twice
MAX_ACTIVE_LOANS_PER_MEMBER = 5    # borrowing cap enforced at checkout time
MAX_OUTSTANDING_FINES_TO_BORROW = Decimal("10.00")  # fines above this block new borrowing

FINE_PER_DAY = Decimal("0.50")     # flat daily fine rate
FINE_GRACE_DAYS = 0                # days overdue before a fine starts accruing
MAX_FINE_PER_LOAN = Decimal("20.00")  # fine is capped so it can't grow forever

# Assumed BookCopy.status values (owned by catalog.models.BookCopy, Cecil's
# module) -- kept here as named constants so nothing in circulation hard-codes
# the raw strings, and so the two modules only need to agree once.
COPY_STATUS_AVAILABLE = "AVAILABLE"
COPY_STATUS_ON_LOAN = "BORROWED"


def calculate_due_date(
    issue_date: datetime.datetime | datetime.date, renewal_count: int = 0
) -> datetime.date:
    """
    Due date = issue date + standard loan period + one renewal period
    per renewal already granted.

    Accepts either a datetime or a date for `issue_date` so it can be
    called directly with `Loan.issue_date` (a DateTimeField).
    """
    issue_day = issue_date.date() if hasattr(issue_date, "date") else issue_date
    total_days = LOAN_PERIOD_DAYS + (renewal_count * RENEWAL_PERIOD_DAYS)
    return issue_day + datetime.timedelta(days=total_days)


def days_overdue(loan) -> int:
    """
    Whole days between the due date and the relevant 'as of' date:
    - if the loan has been returned, count up to the return date only
      (so a returned loan's fine never keeps climbing);
    - otherwise count up to today.
    """
    as_of = loan.return_date.date() if loan.return_date else timezone.localdate()
    delta = (as_of - loan.due_date).days
    return max(delta, 0)


def calculate_fine(loan) -> Decimal:
    """
    Flat per-day fine after an optional grace period, capped at
    MAX_FINE_PER_LOAN. Safe to call at any point in a loan's life:
    returns Decimal('0.00') for loans that are not overdue.
    """
    overdue_days = days_overdue(loan)
    billable_days = max(overdue_days - FINE_GRACE_DAYS, 0)
    fine = Decimal(billable_days) * FINE_PER_DAY
    return min(fine, MAX_FINE_PER_LOAN).quantize(Decimal("0.01"))


def can_renew(loan) -> Tuple[bool, str]:
    """
    Returns (eligible, reason). `reason` is a human-readable string,
    populated only when eligible is False, so Leona's views/templates
    can display it directly to the student without extra translation.
    """
    if loan.is_returned:
        return False, "This loan has already been returned."
    if loan.renewal_count >= MAX_RENEWALS:
        return False, f"This loan has already been renewed {MAX_RENEWALS} times."
    if loan.is_overdue:
        return False, "Overdue loans must be returned (and any fine paid) before renewing."
    return True, ""


def can_borrow(
    active_loan_count: int, outstanding_fines: Decimal, copy_is_available: bool
) -> Tuple[bool, str]:
    """
    Gate checked before creating a new Loan (called from the borrow view
    in Leona's module, after it has already picked a specific BookCopy).
    Counts, totals and the copy's availability are passed in rather than
    queried here, so this function stays a pure, easily-testable unit
    with no database access of its own. `start_loan` on the Loan model
    still performs a final atomic compare-and-swap on the copy's status,
    since this check alone can't prevent a race between two requests.
    """
    if not copy_is_available:
        return False, "This copy is not currently available."
    if active_loan_count >= MAX_ACTIVE_LOANS_PER_MEMBER:
        return False, f"You already have {MAX_ACTIVE_LOANS_PER_MEMBER} books on loan."
    if outstanding_fines > MAX_OUTSTANDING_FINES_TO_BORROW:
        return (
            False,
            f"Outstanding fines (GHS {outstanding_fines}) must be paid before borrowing.",
        )
    return True, ""
