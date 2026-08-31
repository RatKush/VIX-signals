"""
The signal board.

For every structure, today's live curve gives a deviation from the fitted curve.
The fit is seasonally adjusted -- each contract's persistent month offset is
removed before the smooth curve is fitted -- so the deviation is a genuine
mispricing rather than a calendar artefact. See `seasonal.py` for why that
matters: the December contract prints below a plain smooth fit in 98% of
sessions and in every year of the sample, so on an unadjusted fit it is a
permanent false signal, and it drags its neighbours' residuals the other way.

The trigger is a single rolling-252 z-score of that adjusted deviation. The
same-anchor-month z-score is reported for transparency but no longer gates
anything: once the seasonality is removed at the contract level, confirming
against a month baseline corrects for it twice, and measured across the signal
set it slightly reduced total edge (+1736 against +1788 ticks a year) while
costing a third of the opportunities.

Verdicts, in order of precedence:

    NOT TRADED    the structure is a carry vehicle; its deviation signal has
                  zero or negative incremental edge over simply selling it
    VETO          firings with this anchor month historically failed to travel
                  far enough to reach the bracket. A backstop -- the offsets
                  should already have removed the usual seasonal false signals
    NO EDGE       calibration says this structure does not pay after costs
    HALF SIZE     the month offsets are not yet estimated, so the structure is
                  being scored on the unadjusted fit
    DUPLICATE     fired, but it is the same view as another fired structure
                  sharing its legs -- take the better expression, not both
    TRADE         trigger fired on the adjusted fit, structure calibrated positive
    WATCH         |z| between the watch and entry thresholds
    FLAT          nothing doing
"""

from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd

from . import seasonal
from .curve import (fit_curve, fit_curve_adjusted, static_roll_carry,
                    structure_deviation)
from .history import ENTRY_Z, STOP_SIGMA, TARGET_SIGMA, TIME_STOP, History
from .live import LiveCurve
from .structures import STRUCTURES, TICK_VALUE_USD, anchor_leg

WATCH_Z = 1.0
MONTH_AGREE_Z = 0.75          # month-z must reach this, with the same sign
MIN_EDGE_TICKS = 0            # calibrated incremental edge must exceed this

# A firing is vetoed when this anchor month's historical firings did not
# typically travel far enough to reach the bracket. Expressed as a fraction of
# the structure's own median target so the rule is scale-free -- an absolute
# tick threshold would veto small structures and wave large ones through.
SEASONAL_VETO_FRACTION = 0.55
SEASONAL_VETO_MIN_OBS = 8


def live_residuals(curve: LiveCurve, month_offset: dict[int, float] | None = None):
    """
    Today's residuals on both bases.

    Returns (plain, adjusted, offsets, fitted) as {generic: ticks} mappings.
    `adjusted` is the signal basis: the contract-month offsets are removed from
    the prices before the smooth curve is refitted, so a structurally cheap
    month (December) neither reports a residual it always has nor drags its
    neighbours' residuals the other way.
    """
    gens = curve.generics
    if len(gens) < 4:
        return None, None, None, None
    dtes = np.array([curve.dte[g] for g in gens], float)
    prices = np.array([curve.prices[g] for g in gens], float)

    fitted_plain, res_plain = fit_curve(dtes, prices, curve.spot_vix)
    if res_plain is None:
        return None, None, None, None
    plain = {g: float(r) * 100.0 for g, r in zip(gens, res_plain)}

    offsets, adjusted, fitted = None, None, None
    if month_offset:
        months = [curve.expiry[g].month if curve.expiry.get(g) else None for g in gens]
        off = seasonal.offsets_for(months, month_offset)
        if not np.isnan(off).any():
            fit_adj, res_adj = fit_curve_adjusted(dtes, prices, curve.spot_vix, off)
            if res_adj is not None:
                offsets = {g: float(o) for g, o in zip(gens, off)}
                adjusted = {g: float(r) * 100.0 for g, r in zip(gens, res_adj)}
                fitted = {g: float(f) for g, f in zip(gens, fit_adj)}
    if fitted is None:
        fitted = {g: float(f) for g, f in zip(gens, fitted_plain)}
    return plain, adjusted, offsets, fitted


def _z_from_history(hist: History, name: str, dev_now: float):
    """
    Score today's deviation against the historical distributions.

    Returns (z_roll, z_month, mean, sd) -- the mean and standard deviation of
    the trailing deviation window come back too, because the chart's trigger
    band has to be drawn from the SAME two numbers the z-score uses. Drawing it
    from anything else (the structure's daily P&L sigma, say) puts points
    outside a band that did not fire, which makes the encoding unreadable.
    """
    d = hist.deviation.get(name)
    if d is None or d.dropna().empty:
        return None, None, None, None
    tail = d.dropna().iloc[-252:]
    mu, sd = float(tail.mean()), float(tail.std())
    z_roll = (dev_now - mu) / sd if sd else None

    am = hist.anchor_month.get(name)
    z_month = None
    if am is not None:
        month_now = _anchor_month_now(name)
        if month_now is not None:
            same = d[am == month_now].dropna()
            if len(same) >= 60 and same.std():
                z_month = (dev_now - same.mean()) / same.std()
    return (float(z_roll) if z_roll is not None and np.isfinite(z_roll) else None,
            float(z_month) if z_month is not None and np.isfinite(z_month) else None,
            mu if np.isfinite(mu) else None,
            sd if np.isfinite(sd) else None)


# Structures are named by the calendar month of their FRONT leg rather than by
# generic index, because G3 means a different contract every month while "Nov"
# does not. Family plus front month identifies a consecutive-leg structure
# uniquely; the wide calendars and skip-flies share a front leg with others, so
# those spell out every leg.
_FAMILY_SHORT = {
    "1:1 calendar": "CAL",
    "fly 1:2:1": "FLY",
    "skip-fly": "SKIP",
    "ratio 1:2": "1:2",
    "ratio 2:3": "2:3",
}


def _mmm(label: str | None) -> str:
    """'Sep 26' -> 'Sep'."""
    return label.split()[0] if label else "?"


def display_name(st, curve: LiveCurve) -> str:
    months = [_mmm(curve.label.get(g)) for g in st.legs]
    fam = _FAMILY_SHORT.get(st.family, st.family)
    consecutive = list(st.legs) == list(range(st.legs[0], st.legs[0] + len(st.legs)))
    # consecutive legs are implied by the family, so the front month is enough;
    # anything else would collide (CAL Sep is G1G2, G1G3, G1G4 and G1G6 alike)
    return f"{fam} {months[0]}" if consecutive else f"{fam} {'/'.join(months)}"


_ANCHOR_MONTH_NOW: dict[str, int] = {}


def _anchor_month_now(name: str) -> int | None:
    return _ANCHOR_MONTH_NOW.get(name)


def build_board(hist: History, curve: LiveCurve,
                entry_z: float = ENTRY_Z, watch_z: float = WATCH_Z,
                residuals=None) -> pd.DataFrame:
    """
    `residuals` is an optional (plain, adjusted, offsets, fitted) tuple from
    `live_residuals`. The server draws the curve from the same fit it scores on,
    so it passes one in rather than letting the fit happen twice and drift.
    """
    plain, adjusted, _, _ = residuals if residuals is not None else         live_residuals(curve, getattr(hist, "month_offset", None))
    if plain is None:
        return pd.DataFrame()
    residuals = adjusted if adjusted is not None else plain
    seasonally_adjusted = adjusted is not None

    gens = curve.generics
    dtes = np.array([curve.dte[g] for g in gens], float)
    prices = np.array([curve.prices[g] for g in gens], float)

    _ANCHOR_MONTH_NOW.clear()
    for name, st in STRUCTURES.items():
        g = anchor_leg(st)
        if g in curve.expiry:
            _ANCHOR_MONTH_NOW[name] = curve.expiry[g].month

    cal = hist.calibration
    rows = []
    for name, st in STRUCTURES.items():
        if any(g not in curve.prices for g in st.legs):
            continue
        value = st.value(curve.prices)
        dev = structure_deviation(st.weights, st.legs, residuals)
        dev_raw = structure_deviation(st.weights, st.legs, plain)
        if dev is None:
            continue
        carry = static_roll_carry(dtes, prices, curve.spot_vix, st.weights, st.legs)
        z_roll, z_month, dev_mu, dev_sd = _z_from_history(hist, name, dev)

        c = cal.loc[name] if name in cal.index else None
        sigma = float(hist.sigma[name].dropna().iloc[-1]) if name in hist.sigma and \
            not hist.sigma[name].dropna().empty else None
        target = round(TARGET_SIGMA * sigma, 1) if sigma else None
        stop = round(STOP_SIGMA * sigma, 1) if sigma else None

        anchor_m = _ANCHOR_MONTH_NOW.get(name)
        month_note = ""
        ms = hist.month_stats.get(name, {}).get(anchor_m) if anchor_m else None
        if ms:
            month_note = f"{ms['median_mfe']:.0f} tick median move, n={ms['n']}"

        side = None
        if z_roll is not None and abs(z_roll) >= entry_z:
            side = "SELL" if z_roll > 0 else "BUY"

        verdict, why = _verdict(st, c, z_roll, z_month, anchor_m, entry_z, watch_z,
                                ms, seasonally_adjusted)

        rows.append({
            "structure": name,
            "display": display_name(st, curve),
            "family": st.family,
            "legs": st.n_legs,
            "gross_contracts": st.gross_contracts,
            "cost_usd": st.cost_usd,
            "recognised": st.recognised_spread,
            "value_ticks": None if value is None else round(value, 1),
            "deviation_ticks": round(dev, 1),
            "deviation_raw_ticks": None if dev_raw is None else round(dev_raw, 1),
            "seasonally_adjusted": seasonally_adjusted,
            "carry_ticks_day": None if carry is None else round(carry, 2),
            "z_roll": None if z_roll is None else round(z_roll, 2),
            "z_month": None if z_month is None else round(z_month, 2),
            # the two numbers the trigger band is drawn from, so the chart and
            # the z-score can never disagree
            "dev_mean_ticks": None if dev_mu is None else round(dev_mu, 1),
            "dev_sd_ticks": None if dev_sd is None else round(dev_sd, 1),
            "anchor_month": anchor_m,
            "anchor_label": curve.label.get(anchor_leg(st)),
            "side": side,
            "sigma_ticks": None if sigma is None else round(sigma, 1),
            "target_ticks": target,
            "stop_ticks": stop,
            "target_usd": None if target is None else round(target * TICK_VALUE_USD),
            "stop_usd": None if stop is None else round(stop * TICK_VALUE_USD),
            "hit_rate": None if c is None else c["hit_rate"],
            "exp_ticks": None if c is None else c["exp_ticks"],
            "edge_ticks": None if c is None else c["edge_ticks"],
            "per_year": None if c is None else c["per_year"],
            "median_sessions": None if c is None else c["median_sessions"],
            "signal_traded": st.signal_traded,
            "carry_traded": st.carry_traded,
            "verdict": verdict,
            "why": why,
            "month_note": month_note,
            "note": st.note,
        })

    board = pd.DataFrame(rows)
    if board.empty:
        return board
    board = _flag_overlaps(board)
    order = {"TRADE": 0, "HALF SIZE": 1, "DUPLICATE": 2, "VETO": 3, "WATCH": 4,
             "NO EDGE": 5, "NOT TRADED": 6, "FLAT": 7}
    board["_o"] = board["verdict"].map(order).fillna(9)
    board["_a"] = board["z_roll"].abs().fillna(0)
    board = board.sort_values(["_o", "_a"], ascending=[True, False]).drop(columns=["_o", "_a"])
    return board.reset_index(drop=True)


OVERLAP_MIN_SHARED_LEGS = 2


def _flag_overlaps(board: pd.DataFrame) -> pd.DataFrame:
    """
    Group fired signals that are the same view wearing different weights.

    A fly, a 1:2 and a 2:3 on the same three generics share every leg and move
    together -- taking all three is one trade at triple size, not three trades.
    Within each group the highest calibrated expectancy per trade is marked as
    the expression to use and the rest are demoted to DUPLICATE, so the board
    cannot be read as independent opportunities.
    """
    fired = board[board["verdict"].isin(("TRADE", "HALF SIZE"))].index.tolist()
    if len(fired) < 2:
        board["overlap_group"] = None
        board["preferred"] = board["verdict"].isin(("TRADE", "HALF SIZE"))
        return board

    legs = {i: set(STRUCTURES[board.at[i, "structure"]].legs) for i in fired}
    sides = {i: board.at[i, "side"] for i in fired}

    parent = {i: i for i in fired}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a in fired:
        for b in fired:
            if a >= b:
                continue
            if (len(legs[a] & legs[b]) >= OVERLAP_MIN_SHARED_LEGS
                    and sides[a] == sides[b]):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    groups: dict[int, list[int]] = {}
    for i in fired:
        groups.setdefault(find(i), []).append(i)

    board["overlap_group"] = None
    board["preferred"] = False
    for gid, (root, members) in enumerate(groups.items(), start=1):
        label = f"G{gid}" if len(members) > 1 else None
        # prefer the largest calibrated expectancy, then the cheapest to trade
        def quality(i):
            e = board.at[i, "exp_ticks"]
            e = -1e9 if e is None or (isinstance(e, float) and not np.isfinite(e)) else e
            return (e, -board.at[i, "gross_contracts"])
        best = max(members, key=quality)
        for i in members:
            board.at[i, "overlap_group"] = label
            board.at[i, "preferred"] = (i == best)
            if len(members) > 1 and i != best:
                board.at[i, "verdict"] = "DUPLICATE"
                board.at[i, "why"] = (
                    f"same view as {board.at[best, 'display']} "
                    f"({len(legs[i] & legs[best])} shared legs, same side) -- "
                    f"trade one expression, not both")
    return board


_MONTH_NAME = ("", "January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")


def _verdict(st, cal_row, z_roll, z_month, anchor_month, entry_z, watch_z,
             month_stat, adjusted=True):
    if not st.signal_traded:
        return "NOT TRADED", "carry vehicle -- deviation signal has no incremental edge"
    if z_roll is None:
        return "FLAT", "insufficient history to score"

    fired = abs(z_roll) >= entry_z
    if not fired:
        if abs(z_roll) >= watch_z:
            return "WATCH", f"z {z_roll:+.2f}, approaching {entry_z:+.1f}"
        return "FLAT", f"z {z_roll:+.2f}"

    # Seasonal veto: this anchor month's firings historically failed to travel
    # far enough to reach the bracket. December on the deferred curve is the
    # case this was built for -- the year-end kink is persistent, not a
    # dislocation -- but the rule is measured, not hard-coded to one month.
    if cal_row is not None and month_stat is not None:
        target = cal_row.get("median_target")
        if (target and month_stat["n"] >= SEASONAL_VETO_MIN_OBS
                and month_stat["median_mfe"] < SEASONAL_VETO_FRACTION * target):
            mn = _MONTH_NAME[anchor_month] if anchor_month else "this"
            return "VETO", (f"{mn} anchor: past firings moved a median "
                            f"{month_stat['median_mfe']:.0f} ticks against this "
                            f"structure's historical {target:.0f}-tick target, "
                            f"n={month_stat['n']}")

    if cal_row is None:
        return "HALF SIZE", "no calibration for this structure"
    edge = cal_row["edge_ticks"]
    if edge is None or (isinstance(edge, float) and not np.isfinite(edge)) or edge <= MIN_EDGE_TICKS:
        return "NO EDGE", "calibrated incremental edge is not positive"
    exp = cal_row["exp_ticks"]
    if exp is None or (isinstance(exp, float) and not np.isfinite(exp)):
        return "HALF SIZE", "calibrated expectancy is unavailable for this structure"
    if exp <= 0:
        return "NO EDGE", "calibrated expectancy is negative after costs"

    if not adjusted:
        return "HALF SIZE", ("contract-month offsets not yet estimated -- scored on "
                            "the unadjusted fit, so half size")
    return "TRADE", (f"z {z_roll:+.2f} on the seasonally adjusted fit, "
                     f"E[{cal_row['exp_ticks']:+.1f}] ticks a trade")


def regime_panel(hist: History, curve: LiveCurve) -> dict:
    idx = hist.indices
    last = {}
    for col in ("VIX", "VIX9D", "VIX3M", "VIX6M", "VVIX", "VIX1D"):
        if col in idx.columns:
            s = idx[col].dropna()
            if len(s):
                last[col] = {"value": round(float(s.iloc[-1]), 2),
                             "as_of": s.index[-1].strftime("%Y-%m-%d")}
    spot = curve.spot_vix
    g1 = curve.prices.get(1)
    g2 = curve.prices.get(2)
    basis = None if (spot is None or g1 is None) else round(g1 - spot, 2)
    roll = None
    if basis is not None and curve.dte.get(1):
        roll = round(basis / max(curve.dte[1], 1), 3)
    ivts = None
    if spot and "VIX3M" in last:
        ivts = round(spot / last["VIX3M"]["value"], 3)
    fly = None
    if all(curve.prices.get(g) for g in (1, 2, 3)):
        fly = round(((curve.prices[2] - curve.prices[1]) -
                     (curve.prices[3] - curve.prices[2])) * 100.0, 1)

    vix_pct = None
    if spot and "VIX" in idx.columns:
        s = idx["VIX"].dropna().iloc[-252:]
        if len(s):
            vix_pct = round(100.0 * float((s < spot).mean()), 1)

    return {"indices": last, "spot_vix": spot, "spot_source": curve.spot_source,
            "basis_ticks": None if basis is None else round(basis * 100, 1),
            "daily_roll_ticks": None if roll is None else round(roll * 100, 1),
            "ivts": ivts, "front_fly_ticks": fly, "vix_pct_1y": vix_pct,
            "expectancy_band": _vix_band(spot)}


def _vix_band(vix: float | None) -> str:
    if vix is None:
        return "unknown"
    if vix < 13:
        return "below 13 -- short-vol expectancy is negative here"
    if vix < 16:
        return "13-16 -- short-vol expectancy is near zero"
    if vix < 20:
        return "16-20 -- modest positive expectancy"
    if vix < 25:
        return "20-25 -- strong expectancy"
    return "above 25 -- strongest expectancy, largest tail"
