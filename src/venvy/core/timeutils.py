"""Timezone-aware datetime helpers.

Venvy stores every timestamp in UTC, but SQLite hands back naive datetimes, so
values read from the database must be normalised before they are compared to
:func:`utc_now`.
"""

from __future__ import annotations

import datetime

__all__ = ["make_tz_aware", "utc_now"]


def utc_now() -> datetime.datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.UTC)


def make_tz_aware(value: datetime.datetime) -> datetime.datetime:
    """Return *value* with UTC attached if it is naive.

    Naive datetimes are assumed to already be UTC, which holds for every column
    Venvy writes because they all originate from :func:`utc_now`.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
