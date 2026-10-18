"""Five-field cron expressions: minute hour day-of-month month day-of-week.

Supports `*`, lists (1,15), ranges (1-5), steps (*/10, 0-30/5), month and
weekday names, Sunday as 0 or 7, and the @hourly @daily @midnight @weekly
@monthly @yearly shorthands. As in cron, when both day-of-month and
day-of-week are restricted, a day matching either one counts."""
import datetime
from typing import Set, Tuple

_MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *",
}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
# (name, lowest, highest)
_FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7)]


class CronError(ValueError):
    pass


def _number(token: str, name: str, lo: int, hi: int) -> int:
    t = token.lower()
    if name == "month" and t in _MONTHS:
        return _MONTHS.index(t) + 1
    if name == "day of week" and t in _DAYS:
        return _DAYS.index(t)
    if not t.isdigit():
        raise CronError(f"'{token}' is not a valid {name}.")
    n = int(t)
    if not lo <= n <= hi:
        raise CronError(f"{name} must be between {lo} and {hi}, not {n}.")
    return n


def _field(text: str, name: str, lo: int, hi: int) -> Tuple[Set[int], bool]:
    """(allowed values, whether the field starts with * — as in cron, */2 counts as * for
    the day-of-month / day-of-week rule)."""
    values: Set[int] = set()
    star = text.startswith("*")
    for part in text.split(","):
        if not part:
            raise CronError(f"The {name} field has an empty item.")
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"'{s}' is not a valid step in the {name} field.")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            a, b = _number(x, name, lo, hi), _number(y, name, lo, hi)
            if a > b:
                raise CronError(f"The range {part} in the {name} field runs backwards.")
        else:
            a = _number(part, name, lo, hi)
            b = hi if step > 1 else a
        values.update(range(a, b + 1, step))
    if name == "day of week":
        values = {0 if v == 7 else v for v in values}
    return values, star


class Cron:
    def __init__(self, expr: str):
        text = " ".join(expr.split())
        text = _MACROS.get(text.lower(), text)
        parts = text.split(" ")
        if len(parts) != 5:
            raise CronError("A schedule has five fields: minute hour day-of-month month day-of-week (e.g. 30 2 * * *).")
        parsed = [_field(p, n, lo, hi) for p, (n, lo, hi) in zip(parts, _FIELDS)]
        (self.minutes, _), (self.hours, _), (self.dom, self.dom_star), (self.months, _), (self.dow, self.dow_star) = parsed
        self.expr = text

    def matches(self, dt: datetime.datetime) -> bool:
        return (dt.minute in self.minutes and dt.hour in self.hours and dt.month in self.months
                and self._day_ok(dt))

    def next_after(self, dt: datetime.datetime, limit_days: int = 366 * 5) -> datetime.datetime:
        """The first matching minute strictly after *dt*."""
        t = dt.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        end = t + datetime.timedelta(days=limit_days)
        while t < end:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + datetime.timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + datetime.timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + datetime.timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += datetime.timedelta(minutes=1)
                continue
            return t
        raise CronError("That schedule never runs.")

    def _day_ok(self, t: datetime.datetime) -> bool:
        dom_ok = t.day in self.dom
        dow_ok = (t.isoweekday() % 7) in self.dow
        # As in cron: a field starting with * narrows the other (a bare * matches every day);
        # only when both are restricted does a day matching either count.
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


def parse(expr: str) -> Cron:
    return Cron(expr)


def validate(expr: str) -> str:
    """The normalised expression; raises CronError. Also refuses one that never runs."""
    c = Cron(expr)
    c.next_after(datetime.datetime(2024, 1, 1))
    return c.expr
