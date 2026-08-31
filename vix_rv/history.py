"""
Historical panel: generic curve, deviations, same-contract P&L, and the
per-structure calibration the live board reads its target, stop and hit rate
from.

Everything here is expensive to compute and changes once a day, so the whole
object is pickled to cache and rebuilt only when the underlying data moves.
"""

from __future__ import annotations

import datetime as _dt
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar_vx import trading_days_vector
from .cboe import CboeCache
from . import seasonal
from .curve import fit_curve, fit_curve_adjusted
from .structures import STRUCTURES, Structure, anchor_leg

N_GENERIC = 8
Z_WINDOW = 252            # rolling window for the trigger z-score
MONTH_MIN_OBS = 60        # minimum same-month observations before month-z is valid
SIGMA_WINDOW = 60         # trailing window for a structure's own daily sigma
ENTRY_Z = 1.5
TARGET_SIGMA = 2.0
STOP_SIGMA = 1.0
TIME_STOP = 15
SLIPPAGE_TICKS = 1.0      # on top of commission
FIRST_DATE = "2013-01-02"


@dataclass
class History:
    dates: pd.DatetimeIndex
    prices: pd.DataFrame          # generic -> settle
    dte: pd.DataFrame             # generic -> trading days to expiry
    expiry: pd.DataFrame          # generic -> expiry timestamp
    indices: pd.DataFrame         # VIX, VIX3M, VVIX ...
    residuals: pd.DataFrame       # generic -> PLAIN curve-fit residual (ticks)
    offsets: pd.DataFrame         # generic -> causal contract-month offset (ticks)
    residuals_adj: pd.DataFrame   # generic -> seasonally adjusted residual (ticks)
    deviation: pd.DataFrame       # structure -> ADJUSTED deviation, ticks (primary)
    deviation_raw: pd.DataFrame   # structure -> unadjusted deviation, for display
    value: pd.DataFrame           # structure -> raw value in ticks
    pnl: pd.DataFrame             # structure -> next-session P&L, ticks, long
    sigma: pd.DataFrame           # structure -> trailing daily sigma, ticks
    z_roll: pd.DataFrame          # structure -> rolling-252 z of adjusted deviation
    z_month: pd.DataFrame         # structure -> same-anchor-month z (display only)
    anchor_month: pd.DataFrame    # structure -> calendar month of anchor leg
    calibration: pd.DataFrame     # per-structure historical stats
    month_stats: dict             # structure -> {month: median MFE}
    month_offset: dict            # calendar month -> offset in ticks, for live use
    month_effect: pd.DataFrame     # diagnostic: the seasonality being removed
    built_at: _dt.datetime


# ------------------------------------------------------------------ panel

def _generic_panel(contracts: pd.DataFrame):
    """Wide matrices of settle / dte / expiry indexed by date, columns G1..Gn."""
    wide = contracts.pivot_table(index="date", columns="expiry", values="settle").sort_index()
    wide = wide[wide.index >= FIRST_DATE]
    expiries = np.array(sorted(wide.columns))

    dates = wide.index
    px = pd.DataFrame(index=dates, columns=range(1, N_GENERIC + 1), dtype=float)
    ex = pd.DataFrame(index=dates, columns=range(1, N_GENERIC + 1), dtype="datetime64[ns]")

    for dt in dates:
        row = wide.loc[dt]
        live = [e for e in expiries if e > np.datetime64(dt) and pd.notna(row.get(e))][:N_GENERIC]
        for k, e in enumerate(live, start=1):
            px.at[dt, k] = row[e]
            ex.at[dt, k] = e

    keep = px.notna().all(axis=1)
    px, ex = px[keep], ex[keep]

    dte = pd.DataFrame(index=px.index, columns=px.columns, dtype=float)
    for k in px.columns:
        dte[k] = trading_days_vector(px.index, ex[k])
    return px, dte, ex


def _residuals(px: pd.DataFrame, dte: pd.DataFrame, vix: pd.Series) -> pd.DataFrame:
    """Plain (unadjusted) residuals in TICKS, used to estimate the month offsets."""
    res = pd.DataFrame(index=px.index, columns=px.columns, dtype=float)
    gens = list(px.columns)
    vix = vix.reindex(px.index)
    for dt in px.index:
        _, r = fit_curve(dte.loc[dt, gens].to_numpy(float),
                         px.loc[dt, gens].to_numpy(float),
                         vix.get(dt))
        if r is not None:
            res.loc[dt, gens] = r * 100.0
    return res


def _residuals_adjusted(px: pd.DataFrame, dte: pd.DataFrame, vix: pd.Series,
                        offsets: pd.DataFrame) -> pd.DataFrame:
    """
    Second-stage residuals in TICKS: the month offsets are removed from the
    prices, the smooth curve is refitted, and the residual is taken from that.
    Sessions where any offset is still unestimated are left blank rather than
    scored against an incomplete correction.
    """
    res = pd.DataFrame(index=px.index, columns=px.columns, dtype=float)
    gens = list(px.columns)
    vix = vix.reindex(px.index)
    for dt in px.index:
        off = offsets.loc[dt, gens].to_numpy(float)
        if np.isnan(off).any():
            continue
        _, r = fit_curve_adjusted(dte.loc[dt, gens].to_numpy(float),
                                  px.loc[dt, gens].to_numpy(float),
                                  vix.get(dt), off)
        if r is not None:
            res.loc[dt, gens] = r * 100.0
    return res


def _same_contract_pnl(px: pd.DataFrame, ex: pd.DataFrame,
                       contracts: pd.DataFrame) -> pd.DataFrame:
    """
    Next-session P&L of holding one unit LONG of each structure, marked on the
    SAME expiries identified today. Contract rolls therefore introduce no
    artificial jump -- the number is what a real position would have made.
    """
    wide = contracts.pivot_table(index="date", columns="expiry", values="settle").sort_index()
    dates = list(px.index)
    pos = {d: i for i, d in enumerate(dates)}
    out = {name: np.full(len(dates), np.nan) for name in STRUCTURES}

    for i in range(len(dates) - 1):
        d0, d1 = dates[i], dates[i + 1]
        if d1 not in wide.index:
            continue
        row0, row1 = wide.loc[d0], wide.loc[d1]
        # per-generic change on the contract identified at d0
        delta = {}
        for g in px.columns:
            e = ex.at[d0, g]
            if pd.isna(e):
                continue
            p0, p1 = row0.get(e), row1.get(e)
            if pd.notna(p0) and pd.notna(p1):
                delta[g] = float(p1) - float(p0)
        for name, st in STRUCTURES.items():
            if all(g in delta for g in st.legs):
                out[name][i] = sum(w * delta[g] for w, g in zip(st.weights, st.legs)) * 100.0

    return pd.DataFrame(out, index=px.index)


def _zscores(deviation: pd.DataFrame, anchor_month: pd.DataFrame):
    z_roll = (deviation - deviation.rolling(Z_WINDOW).mean()) / deviation.rolling(Z_WINDOW).std()
    z_month = pd.DataFrame(index=deviation.index, columns=deviation.columns, dtype=float)
    for name in deviation.columns:
        d, am = deviation[name], anchor_month[name]
        mu = pd.Series(np.nan, index=d.index)
        sd = pd.Series(np.nan, index=d.index)
        for m in range(1, 13):
            mask = am == m
            if not mask.any():
                continue
            sub = d[mask]
            mu[mask] = sub.expanding(MONTH_MIN_OBS).mean().shift(1)
            sd[mask] = sub.expanding(MONTH_MIN_OBS).std().shift(1)
        z_month[name] = (d - mu) / sd
    return z_roll, z_month


# ------------------------------------------------------- trade simulation

def simulate(z: pd.Series, pnl: pd.Series, sigma: pd.Series, cost_ticks: float,
             side_mode: str = "signal", entry_z: float = ENTRY_Z,
             target_sigma: float = TARGET_SIGMA, stop_sigma: float = STOP_SIGMA,
             time_stop: int = TIME_STOP, seed: int = 0) -> pd.DataFrame:
    """
    Sequential, non-overlapping trades. A signal fires, the position is taken
    against the deviation, and it exits on target, stop or the time stop --
    whichever comes first. Barriers are checked on settlements, so an overnight
    gap can carry the exit past the stop, exactly as it would in practice.
    """
    idx = list(z.index)
    rng = np.random.default_rng(seed)
    rows = []
    i = Z_WINDOW
    n = len(idx)
    while i < n - 1:
        s, sg = z.iloc[i], sigma.iloc[i]
        if not np.isfinite(s) or not np.isfinite(sg) or abs(s) < entry_z:
            i += 1
            continue
        if side_mode == "signal":
            side = -1 if s > 0 else 1
        elif side_mode == "anti":
            side = 1 if s > 0 else -1
        elif side_mode == "random":
            side = int(rng.choice([-1, 1]))
        else:                                   # always-sell baseline
            side = -1
        target, stop = target_sigma * sg, stop_sigma * sg
        cum = mfe = mae = 0.0
        steps = 0
        outcome = "time"
        for k in range(i, min(i + time_stop, n - 1)):
            x = pnl.iloc[k]
            if not np.isfinite(x):
                break
            cum += side * x
            steps += 1
            mfe, mae = max(mfe, cum), min(mae, cum)
            if cum >= target:
                cum, outcome = target, "target"
                break
            if cum <= -stop:
                cum, outcome = -stop, "stop"
                break
        if steps == 0:
            i += 1
            continue
        rows.append({"date": idx[i], "z": s, "side": side, "target": target,
                     "stop": stop, "gross": cum, "net": cum - cost_ticks,
                     "mfe": mfe, "mae": mae, "sessions": steps, "outcome": outcome})
        i += steps + 1
    return pd.DataFrame(rows)


def _calibrate(z_roll, z_month, pnl, sigma, anchor_month) -> tuple[pd.DataFrame, dict]:
    years = None
    rows, month_stats = [], {}
    for name, st in STRUCTURES.items():
        cost = st.cost_ticks + SLIPPAGE_TICKS
        z = z_roll[name]
        trades = simulate(z, pnl[name], sigma[name], cost, "signal")
        if trades.empty or len(trades) < 25:
            continue
        sell = simulate(z, pnl[name], sigma[name], cost, "sell")
        anti = simulate(z, pnl[name], sigma[name], cost, "anti")
        span = (trades["date"].iloc[-1] - trades["date"].iloc[0]).days / 365.25
        span = max(span, 1.0)
        years = span
        per_yr = len(trades) / span
        ann = trades["net"].mean() * per_yr
        sell_ann = (sell["net"].mean() * len(sell) / span) if len(sell) else np.nan
        anti_ann = (anti["net"].mean() * len(anti) / span) if len(anti) else np.nan
        rows.append({
            "structure": name, "family": st.family, "legs": st.n_legs,
            "gross_contracts": st.gross_contracts, "cost_usd": st.cost_usd,
            "cost_ticks": round(cost, 2), "signal_traded": st.signal_traded,
            "carry_traded": st.carry_traded, "recognised": st.recognised_spread,
            "n_trades": len(trades), "per_year": round(per_yr, 1),
            "hit_rate": round(100 * (trades["outcome"] == "target").mean(), 1),
            "stop_rate": round(100 * (trades["outcome"] == "stop").mean(), 1),
            "time_rate": round(100 * (trades["outcome"] == "time").mean(), 1),
            "exp_ticks": round(trades["net"].mean(), 2),
            "ann_ticks": round(ann, 0),
            "sell_ann_ticks": round(sell_ann, 0) if np.isfinite(sell_ann) else None,
            "anti_ann_ticks": round(anti_ann, 0) if np.isfinite(anti_ann) else None,
            "edge_ticks": round(ann - sell_ann, 0) if np.isfinite(sell_ann) else None,
            "median_sessions": round(trades["sessions"].median(), 1),
            "median_target": round(trades["target"].median(), 1),
            "median_stop": round(trades["stop"].median(), 1),
            "median_mae": round(trades["mae"].median(), 1),
            "note": st.note,
        })
        am = anchor_month[name].reindex(trades["date"]).to_numpy()
        ms = {}
        for m in range(1, 13):
            sel = trades.loc[am == m, "mfe"]
            if len(sel) >= 5:
                ms[m] = {"median_mfe": round(float(sel.median()), 1), "n": int(len(sel))}
        month_stats[name] = ms
    cal = pd.DataFrame(rows).set_index("structure")
    return cal, month_stats


# ------------------------------------------------------------------ build

def build(cache: CboeCache) -> History:
    contracts = cache.load_contracts()
    indices = cache.load_indices()
    px, dte, ex = _generic_panel(contracts)
    vix = indices["VIX"] if "VIX" in indices else pd.Series(dtype=float)

    # Stage 1: plain fit, used only to measure the contract-month seasonality.
    res = _residuals(px, dte, vix)
    contract_month = pd.DataFrame({g: ex[g].dt.month for g in px.columns})
    month_effect = seasonal.summary(res, contract_month)
    offsets, month_offset = seasonal.causal_offsets(res, contract_month)

    # Stage 2: refit with that seasonality removed. This is the signal basis.
    res_adj = _residuals_adjusted(px, dte, vix, offsets)

    value, deviation, deviation_raw, anchor_month = {}, {}, {}, {}
    for name, st in STRUCTURES.items():
        if max(st.legs) > N_GENERIC:
            continue
        value[name] = sum(w * px[g] for w, g in zip(st.weights, st.legs)) * 100.0
        deviation[name] = sum(w * res_adj[g] for w, g in zip(st.weights, st.legs))
        deviation_raw[name] = sum(w * res[g] for w, g in zip(st.weights, st.legs))
        anchor_month[name] = ex[anchor_leg(st)].dt.month
    value = pd.DataFrame(value)
    deviation = pd.DataFrame(deviation)
    deviation_raw = pd.DataFrame(deviation_raw)
    anchor_month = pd.DataFrame(anchor_month)

    pnl = _same_contract_pnl(px, ex, contracts)
    sigma = pnl.rolling(SIGMA_WINDOW, min_periods=SIGMA_WINDOW // 2).std().shift(1)
    z_roll, z_month = _zscores(deviation, anchor_month)
    cal, month_stats = _calibrate(z_roll, z_month, pnl, sigma, anchor_month)

    return History(dates=px.index, prices=px, dte=dte, expiry=ex, indices=indices,
                   residuals=res, offsets=offsets, residuals_adj=res_adj,
                   deviation=deviation, deviation_raw=deviation_raw, value=value,
                   pnl=pnl, sigma=sigma, z_roll=z_roll, z_month=z_month,
                   anchor_month=anchor_month, calibration=cal,
                   month_stats=month_stats, month_offset=month_offset,
                   month_effect=month_effect, built_at=_dt.datetime.now())


def load_or_build(cache: CboeCache, path: str | Path = "cache/history.pkl",
                  max_age_hours: float = 12.0, force: bool = False) -> History:
    path = Path(path)
    if path.exists() and not force:
        age = (_dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600.0
        if age < max_age_hours:
            try:
                with path.open("rb") as fh:
                    return pickle.load(fh)
            except Exception:
                pass
    hist = build(cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(hist, fh)
    return hist
