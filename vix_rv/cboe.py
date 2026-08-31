"""
Cboe data layer -- free, direct, no vendor terminal.

Two endpoints carry everything the signal engine needs:

  per-contract history   cdn.cboe.com/data/us/futures/market_statistics/
                         historical_data/VX/VX_<expiry>.csv
                         open/high/low/close/settle/volume/open-interest for
                         every monthly VX expiry, available from Jan 2013.

  index history          cdn.cboe.com/api/global/us_indices/daily_prices/
                         <IDX>_History.csv
                         VIX back to 1990, plus VIX1D / VIX9D / VIX3M / VIX6M /
                         VVIX / SKEW.

Both are cached on disk. Contract files for expiries already in the past never
change, so they are fetched once and kept; the live board only re-pulls the
handful of contracts still trading plus the index files.
"""

from __future__ import annotations

import concurrent.futures as _cf
import datetime as _dt
import io
import os
from pathlib import Path

import pandas as pd
import requests

from .calendar_vx import monthly_expiries

CONTRACT_URL = ("https://cdn.cboe.com/data/us/futures/market_statistics/"
                "historical_data/VX/VX_{expiry}.csv")
INDEX_URL = ("https://cdn.cboe.com/api/global/us_indices/daily_prices/"
             "{name}_History.csv")
SETTLEMENT_URL = "https://www.cboe.com/us/futures/market_statistics/settlement/csv/?dt={date}"

INDICES = ("VIX", "VIX1D", "VIX9D", "VIX3M", "VIX6M", "VVIX", "SKEW")

# The free per-contract archive starts with the Jan 2013 expiry.
ARCHIVE_FIRST_YEAR = 2013

_HEADERS = {"User-Agent": "Mozilla/5.0 (vix-rv-dashboard)"}
_TIMEOUT = 30


class CboeCache:
    def __init__(self, root: str | os.PathLike = "cache"):
        self.root = Path(root)
        self.contracts = self.root / "contracts"
        self.indices = self.root / "indices"
        for d in (self.contracts, self.indices):
            d.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- fetch

    def _get(self, url: str) -> str | None:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        text = r.text
        # The CDN answers with an S3 XML error document rather than a 404 when a
        # contract predates the archive or has not been listed yet.
        if text.lstrip().startswith("<?xml") or "AccessDenied" in text[:400]:
            return None
        if "Trade Date" not in text[:200] and "DATE" not in text[:200]:
            return None
        return text

    def contract_path(self, expiry: _dt.date) -> Path:
        return self.contracts / f"VX_{expiry.isoformat()}.csv"

    def fetch_contract(self, expiry: _dt.date, force: bool = False) -> Path | None:
        """Download one expiry's history. Returns the cached path, or None."""
        path = self.contract_path(expiry)
        if path.exists() and not force:
            return path
        text = self._get(CONTRACT_URL.format(expiry=expiry.isoformat()))
        if text is None:
            return None
        path.write_text(text, encoding="utf-8")
        return path

    def sync_contracts(self, today: _dt.date | None = None,
                       workers: int = 12, refresh_live: bool = True) -> dict:
        """
        Ensure every monthly expiry from the archive start to ~2 years ahead is
        cached. Past expiries are immutable so they are fetched once; contracts
        still trading are re-fetched when `refresh_live` is set.
        """
        today = today or _dt.date.today()
        wanted = [e for e in monthly_expiries(ARCHIVE_FIRST_YEAR, today.year + 2)]
        todo: list[tuple[_dt.date, bool]] = []
        for e in wanted:
            cached = self.contract_path(e).exists()
            if not cached:
                todo.append((e, False))
            elif refresh_live and e >= today:
                todo.append((e, True))

        got, missed = 0, 0
        if todo:
            with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self.fetch_contract, e, f): e for e, f in todo}
                for fut in _cf.as_completed(futs):
                    if fut.result() is not None:
                        got += 1
                    else:
                        missed += 1
        return {"requested": len(todo), "fetched": got, "unavailable": missed,
                "cached_total": len(list(self.contracts.glob("VX_*.csv")))}

    def fetch_index(self, name: str, max_age_hours: float = 8.0) -> Path | None:
        path = self.indices / f"{name}_History.csv"
        if path.exists():
            age = (_dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600.0
            if age < max_age_hours:
                return path
        text = self._get(INDEX_URL.format(name=name))
        if text is None:
            return path if path.exists() else None
        path.write_text(text, encoding="utf-8")
        return path

    def sync_indices(self, names=INDICES) -> dict:
        out = {}
        with _cf.ThreadPoolExecutor(max_workers=len(names)) as pool:
            futs = {pool.submit(self.fetch_index, n): n for n in names}
            for fut in _cf.as_completed(futs):
                n = futs[fut]
                out[n] = fut.result() is not None
        return out

    # ----------------------------------------------------------------- read

    def load_contracts(self) -> pd.DataFrame:
        """Long frame: date, expiry, settle, high, low, volume, open_interest."""
        frames = []
        for path in sorted(self.contracts.glob("VX_*.csv")):
            expiry = pd.Timestamp(path.stem[3:])
            try:
                raw = pd.read_csv(path)
            except Exception:
                continue
            if "Trade Date" not in raw.columns:
                continue
            raw.columns = [c.strip() for c in raw.columns]
            # Pre-2014 rows carry Settle == 0 and the real print in Close.
            settle = raw["Settle"].where(raw["Settle"] > 0, raw["Close"])
            frames.append(pd.DataFrame({
                "date": pd.to_datetime(raw["Trade Date"]),
                "expiry": expiry,
                "settle": pd.to_numeric(settle, errors="coerce"),
                "high": pd.to_numeric(raw.get("High"), errors="coerce"),
                "low": pd.to_numeric(raw.get("Low"), errors="coerce"),
                "volume": pd.to_numeric(raw.get("Total Volume"), errors="coerce"),
                "open_interest": pd.to_numeric(raw.get("Open Interest"), errors="coerce"),
            }))
        if not frames:
            raise RuntimeError("No cached VX contract files -- run sync_contracts() first.")
        df = pd.concat(frames, ignore_index=True)
        df = df[(df["settle"] > 0) & (df["expiry"] > df["date"])]
        return df.sort_values(["date", "expiry"]).reset_index(drop=True)

    def load_index(self, name: str) -> pd.Series:
        path = self.indices / f"{name}_History.csv"
        if not path.exists():
            return pd.Series(dtype=float, name=name)
        raw = pd.read_csv(path)
        raw.columns = [c.strip().upper() for c in raw.columns]
        date_col = "DATE" if "DATE" in raw.columns else raw.columns[0]
        val_col = "CLOSE" if "CLOSE" in raw.columns else raw.columns[-1]
        s = pd.Series(pd.to_numeric(raw[val_col], errors="coerce").values,
                      index=pd.to_datetime(raw[date_col]), name=name)
        return s[~s.index.duplicated(keep="last")].sort_index().dropna()

    def load_indices(self, names=INDICES) -> pd.DataFrame:
        return pd.DataFrame({n: self.load_index(n) for n in names})

    def settlement_board(self, date: _dt.date) -> pd.DataFrame:
        """
        Whole-board daily settlement for VX and VXM on one date. Useful as an
        end-of-day cross-check against the workbook's RTD prints.
        """
        text = self._get(SETTLEMENT_URL.format(date=date.isoformat()))
        if text is None:
            return pd.DataFrame()
        df = pd.read_csv(io.StringIO(text))
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        return df
