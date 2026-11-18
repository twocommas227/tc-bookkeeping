"""Shared exception types (planner-created; other modules import from here)."""
import sqlite3


class LockedMonthError(ValueError):
    """A write hit a locked month. `.month` is YYYY-MM when known."""

    def __init__(self, message: str = "month is locked", month: str | None = None):
        super().__init__(message)
        self.month = month


class CloseBlocked(ValueError):
    """close_month refused. `.items` = list of failed check-item dicts (see docs/UI.md 4.3)."""

    def __init__(self, message: str = "close blocked", items: list | None = None):
        super().__init__(message)
        self.items = items or []


def translate_integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    """Turn a month-lock trigger failure into LockedMonthError; otherwise return exc unchanged."""
    msg = str(exc)
    if "locked" in msg.lower():
        return LockedMonthError(msg)
    return exc
