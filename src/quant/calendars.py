"""Versioned US cash-equity sessions, with conservative 20:00 ET availability.

This calendar is deliberately bounded. Unknown venues and years are not guessed.
Early closes do not change the conservative provider settlement cutoff.
"""
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
CALENDAR_VERSION = "xnys-2010-2030-v1"


def _weekday(year, month, weekday, occurrence):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday-first.weekday()) % 7 + 7*(occurrence-1))


def _observed(day):
    return day + timedelta(days={5: -1, 6: 1}.get(day.weekday(), 0))


def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    return date(year, (h+l-7*m+114)//31, (h+l-7*m+114) % 31 + 1)


@lru_cache(maxsize=32)
def holidays(year):
    if not 2010 <= year <= 2030:
        raise ValueError("XNYS calendar supports 2010-2030")
    memorial = date(year, 5, 31)
    memorial -= timedelta(days=memorial.weekday())
    days = {_weekday(year, 1, 0, 3), _weekday(year, 2, 0, 3),
            _easter(year)-timedelta(days=2), memorial,
            _observed(date(year, 7, 4)), _weekday(year, 9, 0, 1),
            _weekday(year, 11, 3, 4), _observed(date(year, 12, 25))}
    new_year = date(year, 1, 1)
    # NYSE does not observe Saturday New Year's Day on the preceding Friday.
    days.add(new_year + timedelta(days=1 if new_year.weekday() == 6 else 0))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    days.update({date(2012, 10, 29), date(2012, 10, 30),
                 date(2018, 12, 5), date(2025, 1, 9)})
    return days


def sessions(start: date, end: date, calendar: str = "XNYS") -> list[date]:
    if calendar != "XNYS":
        raise ValueError("Unsupported calendar")
    if start > end or (end-start).days > 20*366:
        raise ValueError("Invalid calendar range (maximum 20 years)")
    holidays(start.year)
    holidays(end.year)
    return [day for offset in range((end-start).days+1)
            if (day := start+timedelta(days=offset)).weekday() < 5
            and day not in holidays(day.year)]


def latest_completed_session(now: datetime | None = None) -> date:
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    end = now.date() - timedelta(days=now.time() < time(20))
    return sessions(end-timedelta(days=14), end)[-1]


def freshness(observed: str | None, calendar: str | None = "XNYS", now=None):
    if observed is None or calendar != "XNYS":
        return {"status": "unknown", "latestCompletedSession": None,
                "missingSessions": None, "calendar": calendar}
    latest = latest_completed_session(now)
    day = date.fromisoformat(observed)
    return {"status": "current" if day == latest else "stale" if day < latest else "unknown",
            "latestCompletedSession": latest.isoformat(), "calendar": calendar,
            "missingSessions": len(sessions(day+timedelta(days=1), latest)) if day < latest else 0}
