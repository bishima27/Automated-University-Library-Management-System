"""
circulation/admin.py

Owner: Mbuala Wonder Ephraim Mbakadi

Minimal admin registration for Loan and Fine so librarians can inspect
circulation records from Django admin during development and testing.
Frederick's librarian dashboard (custom views/templates) is the intended
day-to-day interface for admins -- this is a lightweight, low-effort
fallback that comes for free once the models exist.
"""
from django.contrib import admin

from .models import Fine, Loan


class FineInline(admin.StackedInline):
    model = Fine
    extra = 0
    readonly_fields = ("assessed_at",)


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "book_copy",
        "borrower",
        "issue_date",
        "due_date",
        "status",
        "renewal_count",
        "return_date",
    )
    list_filter = ("status",)
    search_fields = (
        "book_copy__book__title",
        "borrower__username",
        "borrower__email",
    )
    readonly_fields = ("status",)
    date_hierarchy = "issue_date"
    inlines = [FineInline]
    actions = ["mark_selected_as_returned"]

    @admin.action(description="Mark selected loans as returned")
    def mark_selected_as_returned(self, request, queryset):
        count = 0
        for loan in queryset.filter(return_date__isnull=True):
            loan.mark_returned()
            count += 1
        self.message_user(request, f"{count} loan(s) marked as returned.")


@admin.register(Fine)
class FineAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "amount", "is_paid", "assessed_at", "paid_at")
    list_filter = ("is_paid",)
    search_fields = ("loan__borrower__username", "loan__borrower__email")
    readonly_fields = ("assessed_at",)
    actions = ["mark_selected_as_paid"]

    @admin.action(description="Mark selected fines as paid")
    def mark_selected_as_paid(self, request, queryset):
        count = 0
        for fine in queryset.filter(is_paid=False):
            fine.mark_paid()
            count += 1
        self.message_user(request, f"{count} fine(s) marked as paid.")
