"""Validation and matching for numeric five-field UTC cron expressions."""

from __future__ import annotations

from datetime import datetime, timezone


_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _field(source: str, lower: int, upper: int) -> frozenset[int]:
    values = set()
    for item in source.split(","):
        parts = item.split("/")
        if len(parts) > 2 or not parts[0]:
            raise ValueError("invalid cron field")
        if len(parts) == 2:
            if not parts[1].isdecimal() or int(parts[1]) < 1:
                raise ValueError("invalid cron step")
            step = int(parts[1])
        else:
            step = 1
        base = parts[0]
        if base == "*":
            start, end = lower, upper
        elif "-" in base:
            ends = base.split("-")
            if len(ends) != 2 or not all(part.isdecimal() for part in ends):
                raise ValueError("invalid cron range")
            start, end = map(int, ends)
        elif base.isdecimal():
            start = int(base)
            end = upper if len(parts) == 2 else start
        else:
            raise ValueError("invalid cron value")
        if not lower <= start <= end <= upper:
            raise ValueError("cron value out of range")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def validate_cron(expression: str) -> tuple[frozenset[int], ...]:
    """Validate standard numeric five-field cron syntax; Sunday is 0 or 7."""
    if not isinstance(expression, str) or len(expression) > 100:
        raise ValueError("cron expression must be a string up to 100 characters")
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression must have five fields")
    return tuple(_field(field, *bounds) for field, bounds in zip(fields, _BOUNDS))


def cron_matches(expression: str, when: datetime) -> bool:
    if when.tzinfo is None:
        raise ValueError("cron time must be timezone-aware")
    minute, hour, day, month, weekday = validate_cron(expression)
    when = when.astimezone(timezone.utc)
    day_matches = when.day in day
    week_day = (when.weekday() + 1) % 7
    weekday_matches = week_day in weekday or (week_day == 0 and 7 in weekday)
    fields = expression.split()
    if not fields[2].startswith("*") and not fields[4].startswith("*"):
        date_matches = day_matches or weekday_matches
    else:
        date_matches = day_matches and weekday_matches
    return when.minute in minute and when.hour in hour and when.month in month and date_matches
