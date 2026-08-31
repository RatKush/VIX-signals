"""
VIX futures expiry calendar and trading-day arithmetic.

A VX monthly contract settles on the Wednesday 30 days before the third Friday
of the FOLLOWING calendar month. That rule is exact -- there is no lookup table
to maintain and no dependency on the workbook's `tenure` sheet.

Bloomberg-style generic tickers (UXU26) encode month + year via the standard
futures month codes, so the same rule converts a live RTD ticker straight to an
expiry date.
"""

from __future__ import annotations

import datetime as _dt
from functools import lru_cache

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

# Futures month codes -> calendar month number
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
CODE_FOR_MONTH = {v: k for k, v in MONTH_CODES.items()}


def third_friday(year: int, month: int) -> _dt.date:
    first = _dt.date(year, month, 1)
    offset = (4 - first.weekday()) % 7          # 4 == Friday
    return first + _dt.timedelta(days=offset + 14)


def vix_expiry(year: int, month: int) -> _dt.date:
    """Settlement date of the VX contract for the given contract month."""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return third_friday(ny, nm) - _dt.timedelta(days=30)


def parse_generic_ticker(ticker: str) -> _dt.date | None:
    """'UXU26 Index' / 'UXU26' / 'U26' -> expiry date. None if unparseable."""
    if not isinstance(ticker, str):
        return None
    t = ticker.strip().upper().replace(" INDEX", "").replace("INDEX", "").strip()
    if t.startswith("UX"):
        t = t[2:]
    if len(t) < 3:
        return None
    code, yy = t[0], t[1:3]
    if code not in MONTH_CODES or not yy.isdigit():
        return None
    return vix_expiry(2000 + int(yy), MONTH_CODES[code])


def contract_label(expiry: _dt.date) -> str:
    """Expiry date -> contract month label, e.g. 'Sep 26'."""
    # The contract month is the month the expiry falls in.
    return f"{expiry.strftime('%b')} {expiry.strftime('%y')}"


def monthly_expiries(start_year: int, end_year: int) -> list[_dt.date]:
    return [vix_expiry(y, m) for y in range(start_year, end_year + 1) for m in range(1, 13)]


@lru_cache(maxsize=1)
def _busday_calendar() -> np.busdaycalendar:
    hol = USFederalHolidayCalendar().holidays(start="2004-01-01", end="2035-12-31")
    return np.busdaycalendar(holidays=hol.values.astype("datetime64[D]"))


def trading_days_between(start, end) -> int:
    """Business days from `start` (inclusive) to `end` (exclusive), US holidays removed."""
    s = np.datetime64(pd.Timestamp(start).date(), "D")
    e = np.datetime64(pd.Timestamp(end).date(), "D")
    return int(np.busday_count(s, e, busdaycal=_busday_calendar()))


def trading_days_vector(starts, ends) -> np.ndarray:
    s = np.asarray(pd.to_datetime(starts).values, dtype="datetime64[D]")
    e = np.asarray(pd.to_datetime(ends).values, dtype="datetime64[D]")
    return np.busday_count(s, e, busdaycal=_busday_calendar())
