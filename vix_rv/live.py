"""
Live curve from `vix_live.xlsx`.

The RTD sheet holds one row per generic, already in front-to-back order:

    UXU26 Index | 17.250 | 17.2042
    UXV26 Index | 18.900 | 18.9246
    ...

column A is the Bloomberg generic ticker, B is Last, C is Settle. The ticker
alone fixes the contract month, and the expiry follows from the exchange rule,
so nothing here depends on the workbook's `tenure` formulas being correct.

There are two ways to read it, and the difference matters:

* **Live, out of Excel's memory (preferred).** xlwings attaches to the already
  open workbook and reads the cells at their current value. RTD formulas are
  live objects -- their value exists in Excel's memory and is only written to
  disk on save -- so this is the only way to see a tick that has not been
  saved. The workbook never has to be saved at all.
* **From the file on disk (fallback).** openpyxl with `data_only=True` returns
  the value Excel *cached at its last save*. If the sheet has not been saved
  for four hours, that is four-hour-old data wearing a live board's clothes.

So the live path is tried first on every refresh and the file is only a
fallback, for when Excel is not running. `#N/A` cells for contracts that have
not started trading are dropped rather than allowed to poison the curve.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import tempfile
import threading as _th
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .calendar_vx import contract_label, parse_generic_ticker, trading_days_between

RTD_SHEET = "RTD"
NA_TOKENS = {"#N/A", "#N/A N/A", "N/A", "#VALUE!", "#REF!", "", "NAN", "NONE"}


@dataclass
class LiveCurve:
    as_of: _dt.datetime
    source: str
    spot_vix: float | None
    spot_source: str
    prices: dict[int, float] = field(default_factory=dict)      # generic -> price
    dte: dict[int, int] = field(default_factory=dict)
    expiry: dict[int, _dt.date] = field(default_factory=dict)
    label: dict[int, str] = field(default_factory=dict)
    ticker: dict[int, str] = field(default_factory=dict)
    price_field: str = "Last"
    stale: list[str] = field(default_factory=list)
    # "excel" when read live out of the open workbook's memory, "file" when
    # read from the last-saved copy on disk. Only "file" can go stale.
    source_kind: str = "file"
    source_note: str = ""

    @property
    def generics(self) -> list[int]:
        return sorted(self.prices)

    def as_rows(self) -> list[dict]:
        return [{
            "generic": g, "ticker": self.ticker.get(g), "label": self.label.get(g),
            "price": self.prices[g], "dte": self.dte.get(g),
            "expiry": self.expiry[g].isoformat() if self.expiry.get(g) else None,
        } for g in self.generics]


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    s = str(v).strip()
    if s.upper() in NA_TOKENS:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


EXCEL_SHEET_MAX_ROWS = 400


def _rows_from_excel(workbook: Path, sheet: str = RTD_SHEET):
    """
    Read (ticker, Last, Settle) straight out of the open workbook's memory.

    This is the whole point of the live path: RTD cells hold their value in
    Excel, not in the file, so only an attached read sees the current tick.
    Nothing is saved and nothing is written -- the workbook is read-only here.

    COM is apartment-threaded and this runs from the refresh thread and from
    Flask request threads, so every calling thread must initialise COM itself
    or the first attach raises. Raises if Excel is not running or the workbook
    is not open, which is the caller's signal to fall back to the file.
    """
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass

    import xlwings as xw

    name = workbook.name
    book = None
    for app in xw.apps:
        for bk in app.books:
            try:
                if bk.name == name or str(bk.fullname).lower() == str(workbook).lower():
                    book = bk
                    break
            except Exception:
                continue
        if book is not None:
            break
    if book is None:
        raise RuntimeError(f"{name} is not open in Excel")

    if sheet not in [sh.name for sh in book.sheets]:
        raise RuntimeError(f"{name} has no '{sheet}' sheet")

    # A bounded range rather than used_range: a stray cell far down the sheet
    # would otherwise drag hundreds of empty rows across the COM boundary on
    # every single refresh.
    vals = book.sheets[sheet].range((1, 1), (EXCEL_SHEET_MAX_ROWS, 3)).value
    if vals is None:
        return []
    if vals and not isinstance(vals[0], (list, tuple)):
        vals = [vals]
    rows = []
    for r in vals:
        if r is None:
            continue
        r = list(r) + [None, None, None]
        if r[0] is None:
            continue
        rows.append((r[0], r[1], r[2]))
    return rows


def _snapshot(path: Path) -> Path:
    """Copy the workbook so an open Excel session cannot block the read."""
    tmp = Path(tempfile.gettempdir()) / f"vixlive_{_dt.datetime.now():%H%M%S%f}.xlsx"
    shutil.copy2(path, tmp)
    return tmp


def _rows_from_file(path: Path):
    """The last-saved values. Only as fresh as the most recent Ctrl-S."""
    import openpyxl

    tmp = _snapshot(path)
    try:
        wb = openpyxl.load_workbook(tmp, data_only=True, read_only=True)
        if RTD_SHEET not in wb.sheetnames:
            raise ValueError(f"{path.name} has no '{RTD_SHEET}' sheet "
                             f"(found {wb.sheetnames})")
        ws = wb[RTD_SHEET]
        raw = [(r[0], r[1] if len(r) > 1 else None, r[2] if len(r) > 2 else None)
               for r in ws.iter_rows(values_only=True)]
        wb.close()
    finally:
        tmp.unlink(missing_ok=True)
    return raw


def read_live(workbook: str | Path, price_field: str = "Last",
              spot_vix: float | None = None, spot_source: str = "",
              today: _dt.date | None = None, prefer_excel: bool = True) -> LiveCurve:
    """
    Parse the RTD sheet into a generic curve. `price_field` picks Last (column B)
    or Settle (column C); Last is the right choice intraday, Settle after the
    close.

    With `prefer_excel` the open workbook's in-memory values are used, so the
    board tracks RTD without the sheet ever being saved. If Excel is not running
    the last-saved file is read instead and the curve says so, because a fallback
    that looks identical to the live path is how stale prices get traded.
    """
    path = Path(workbook)
    kind, note = "file", ""
    raw = None

    if prefer_excel:
        try:
            raw = _rows_from_excel(path)
            kind = "excel"
            note = "live from the open workbook"
        except Exception as exc:
            note = f"Excel unavailable ({exc}); read the saved file"
            raw = None

    if raw is None:
        raw = _rows_from_file(path)
        kind = "file"
        if not note:
            note = "read from the saved file"

    today = today or _dt.date.today()
    col = 1 if price_field.lower() == "last" else 2

    # Live values are current as of now; file values are only as new as the
    # last save, so that is the honest timestamp for them.
    as_of = (_dt.datetime.now() if kind == "excel"
             else _dt.datetime.fromtimestamp(path.stat().st_mtime))

    curve = LiveCurve(as_of=as_of,
                      source=str(path), spot_vix=spot_vix, spot_source=spot_source,
                      price_field=price_field, source_kind=kind, source_note=note)

    g = 0
    for ticker, last, settle in raw:
        if ticker is None:
            continue
        expiry = parse_generic_ticker(str(ticker))
        if expiry is None:
            continue
        if expiry <= today:
            curve.stale.append(f"{ticker} expired {expiry}")
            continue
        px = _num((last, settle)[col - 1])
        if px is None:                      # fall back to the other column
            px = _num((last, settle)[2 - col])
        if px is None or px <= 0:
            curve.stale.append(f"{ticker} no price")
            continue
        g += 1
        curve.prices[g] = px
        curve.expiry[g] = expiry
        curve.label[g] = contract_label(expiry)
        curve.ticker[g] = str(ticker).strip()
        curve.dte[g] = trading_days_between(today, expiry)

    return curve


SPOT_CACHE_SECONDS = 60.0
SPOT_TIMEOUT_SECONDS = 6.0
_SPOT_CACHE: dict = {"at": None, "value": None, "source": ""}
_SPOT_LOCK = _th.Lock()


def _yahoo_spot() -> tuple[float | None, str]:
    import yfinance as yf
    px = yf.Ticker("^VIX").fast_info["last_price"]
    return (round(float(px), 2), "yahoo intraday") if px else (None, "")


def fetch_spot_vix(max_age: float = SPOT_CACHE_SECONDS,
                   timeout: float = SPOT_TIMEOUT_SECONDS) -> tuple[float | None, str]:
    """
    Live spot VIX for the curve anchor. Yahoo first because it is intraday;
    the Cboe daily file is the fallback and is a prior close.

    Cached for `max_age` seconds and bounded by `timeout`. Both matter because
    the board refreshes every 20 seconds from a daemon thread that also serves
    /api/refresh: without the cache that is a network round trip per tick, and
    without the timeout a hung Yahoo socket blocks the refresh indefinitely and
    takes every waiting request with it. On timeout the worker is abandoned
    rather than waited on, and the last good value is reused if we have one.
    """
    now = _dt.datetime.now()
    with _SPOT_LOCK:
        at, val = _SPOT_CACHE["at"], _SPOT_CACHE["value"]
        if at is not None and val is not None and (now - at).total_seconds() < max_age:
            return val, _SPOT_CACHE["source"]

    box: dict = {}

    def _work():
        try:
            box["r"] = _yahoo_spot()
        except Exception:
            box["r"] = (None, "")

    t = _th.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    px, src = box.get("r", (None, ""))

    if px is None:
        try:
            from .cboe import CboeCache
            s = CboeCache().load_index("VIX")
            if len(s):
                px, src = round(float(s.iloc[-1]), 2), f"cboe close {s.index[-1]:%Y-%m-%d}"
        except Exception:
            pass

    if px is None:
        with _SPOT_LOCK:
            if _SPOT_CACHE["value"] is not None:
                return _SPOT_CACHE["value"], _SPOT_CACHE["source"] + " (stale)"
        return None, "unavailable"

    with _SPOT_LOCK:
        _SPOT_CACHE.update(at=now, value=px, source=src)
    return px, src
