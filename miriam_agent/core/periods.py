"""Calendar window helpers shared by the financial engine and the Go client.

Lives in core so both the domain layer (which computes on windows) and the
integration layer (which fetches them) can use it without one importing the
other — docs/ARCHITECTURE-CONTRACT.md §3 forbids integrations -> domain.
"""

from __future__ import annotations

from datetime import date, timedelta


def period_to_window(
    period: str, today: date | None = None
) -> tuple[str | None, str | None]:
    """Map a health-audit period to an (from, to) YYYY-MM-DD window.

    Returns (None, None) for unknown periods, which lets the backend use its
    current-calendar-month default.
    """
    today = today or date.today()
    if period == "this_month":
        return today.strftime("%Y-%m-01"), today.strftime("%Y-%m-%d")
    if period == "last_month":
        last_month_last = today.replace(day=1) - timedelta(days=1)
        return last_month_last.strftime("%Y-%m-01"), last_month_last.strftime(
            "%Y-%m-%d"
        )
    if period == "last_90_days":
        return (today - timedelta(days=89)).strftime("%Y-%m-%d"), today.strftime(
            "%Y-%m-%d"
        )
    if period == "last_6_months":
        first = (today - timedelta(days=183)).strftime("%Y-%m-%d")
        return first, today.strftime("%Y-%m-%d")
    if period == "last_12_months":
        first = (today - timedelta(days=365)).strftime("%Y-%m-%d")
        return first, today.strftime("%Y-%m-%d")
    return None, None
