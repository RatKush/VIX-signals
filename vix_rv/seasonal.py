"""
Contract-month seasonal offsets.

The VX curve is not a smooth function of maturity plus noise. Individual
contract months carry persistent levels relative to any smooth shape, because
the calendar period they cover has a persistent amount of realised volatility
in it. Measured over 2013-2026, the median residual from a plain smooth fit by
contract month, in ticks:

    Oct  +13.0     Feb  +9.0     Jan  +7.3     Apr  +7.1     Sep  +4.9
    Jul  +3.1      Nov  +0.8     Mar  +0.4     May  -2.6     Aug  -6.1
    Jun  -6.7      Dec  -31.9

December is the extreme case and it is not marginal: negative in 98% of all
observations and in every single year of the sample -- 2013 through 2026 without
exception. The holiday period simply contains less realised volatility, and the
market has always priced it that way. October is the mirror image, carrying the
crash-season premium.

Treating those as mispricings produces a permanent false signal on every
structure with a December or October leg. So they are estimated and removed
before the curve is fitted.

Estimation is causal: the offset used on any session is the median of residuals
for that contract month observed strictly EARLIER, so nothing in the backtest
sees its own future. A month needs `MIN_OBS` prior observations before it earns
an offset; until then the structures touching it are left unscored rather than
scored against a guess.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_OBS = 24          # prior observations of a contract month before it is scored
ESTIMATOR = "median"  # median resists the 2020 and 2024 October blowouts


def _reduce(values: list[float]) -> float:
    if ESTIMATOR == "mean":
        return float(np.mean(values))
    return float(np.median(values))


def causal_offsets(residual_ticks: pd.DataFrame,
                   contract_month: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, float]]:
    """
    Walk the panel forward, assigning each (session, generic) the offset implied
    by everything seen before it.

    residual_ticks : sessions x generics, residual from the plain fit, in ticks
    contract_month : sessions x generics, calendar month of that contract

    Returns (offset panel in ticks, final month -> offset mapping for live use).
    """
    gens = list(residual_ticks.columns)
    out = pd.DataFrame(index=residual_ticks.index, columns=gens, dtype=float)
    seen: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    current: dict[int, float] = {}

    for dt in residual_ticks.index:
        months = contract_month.loc[dt]
        for g in gens:
            m = months.get(g)
            if pd.notna(m):
                out.at[dt, g] = current.get(int(m), np.nan)
        # only now fold today's observations into the estimate
        resids = residual_ticks.loc[dt]
        for g in gens:
            m, r = months.get(g), resids.get(g)
            if pd.notna(m) and pd.notna(r):
                seen[int(m)].append(float(r))
        for m, vals in seen.items():
            if len(vals) >= MIN_OBS:
                current[m] = _reduce(vals)

    return out, dict(current)


def offsets_for(months, table: dict[int, float]) -> np.ndarray:
    """Offset vector, in ticks, for a sequence of contract months. NaN if unknown."""
    return np.array([table.get(int(m), np.nan) if m is not None and m == m else np.nan
                     for m in months], dtype=float)


def summary(residual_ticks: pd.DataFrame, contract_month: pd.DataFrame) -> pd.DataFrame:
    """Diagnostic table: the month effect the engine is removing."""
    frames = []
    for g in residual_ticks.columns:
        frames.append(pd.DataFrame({"month": contract_month[g].values,
                                    "resid": residual_ticks[g].values}))
    s = pd.concat(frames, ignore_index=True).dropna()
    grp = s.groupby("month")["resid"]
    out = pd.DataFrame({
        "n": grp.size(),
        "mean": grp.mean().round(1),
        "median": grp.median().round(1),
        "sd": grp.std().round(1),
        "pct_negative": (100 * grp.apply(lambda x: (x < 0).mean())).round(0),
    })
    out.index = out.index.astype(int)
    return out
