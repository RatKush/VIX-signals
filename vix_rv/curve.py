"""
The curve fit, and the deviation that everything else is built on.

Each session, fit a smooth shape through the whole board and take each
contract's residual. A structure's deviation is the weighted sum of its legs'
residuals -- which is, by construction, exactly what a fly or a ratio measures:
the part of the curve a smooth shape cannot explain.

    price ~ a + b * sqrt(dte) + c * dte

fitted by weighted least squares through the live generics PLUS spot VIX pinned
at dte = 0. The spot anchor is not cosmetic: dropping it costs about 14 points
of downstream hit rate, because without it the fit is free to drift at the front
where the interesting dislocations live.

Alternative bases (log, quadratic, cubic) move results by a few points and not
by their sign, so the choice of basis is not load-bearing. The anchor is.
"""

from __future__ import annotations

import numpy as np

SPOT_WEIGHT = 2.0


def design(dte: np.ndarray, basis: str = "sqrt") -> np.ndarray:
    d = np.asarray(dte, dtype=float)
    ones = np.ones_like(d)
    if basis == "sqrt":
        return np.column_stack([ones, np.sqrt(d), d])
    if basis == "log":
        return np.column_stack([ones, np.log1p(d), d])
    if basis == "quad":
        return np.column_stack([ones, d, d ** 2])
    if basis == "cubic":
        return np.column_stack([ones, d, d ** 2, d ** 3])
    raise ValueError(f"unknown basis {basis!r}")


def fit_curve(dtes, prices, spot_vix: float | None,
              basis: str = "sqrt", spot_weight: float = SPOT_WEIGHT):
    """
    Fit the smooth curve and return (fitted_prices, residuals) for the supplied
    contracts only -- the spot anchor participates in the fit but is not
    reported back.

    Returns (None, None) when the board is too thin or carries NaNs.
    """
    d = np.asarray(dtes, dtype=float)
    p = np.asarray(prices, dtype=float)
    if d.size == 0 or np.isnan(p).any() or np.isnan(d).any():
        return None, None

    if spot_vix is not None and np.isfinite(spot_vix):
        d_all = np.concatenate([[0.0], d])
        p_all = np.concatenate([[float(spot_vix)], p])
        w = np.ones_like(d_all)
        w[0] = spot_weight
    else:
        d_all, p_all = d, p
        w = np.ones_like(d_all)

    X = design(d_all, basis)
    if X.shape[0] <= X.shape[1]:
        return None, None

    W = np.sqrt(w)[:, None]
    try:
        beta, *_ = np.linalg.lstsq(X * W, p_all * np.sqrt(w), rcond=None)
    except np.linalg.LinAlgError:
        return None, None

    fitted_all = X @ beta
    offset = 1 if (spot_vix is not None and np.isfinite(spot_vix)) else 0
    fitted = fitted_all[offset:]
    return fitted, p - fitted


def fit_curve_adjusted(dtes, prices, spot_vix, offsets_ticks,
                       basis: str = "sqrt", spot_weight: float = SPOT_WEIGHT):
    """
    Two-stage fit. Known contract-month offsets are removed from the prices
    BEFORE the smooth curve is fitted, then the residual is taken from that.

    Removing them first matters twice over. The obvious part is that a contract
    with a persistent offset stops reporting a residual it always had -- the
    December contract has printed below a plain smooth curve in 98% of sessions
    and in every year since 2013, so on a raw fit it is permanently "cheap" and
    permanently a false signal. The less obvious part is that such a contract
    also drags the fitted curve down around its own tenor, which makes its
    neighbours look rich. Both artefacts disappear here.

    `offsets_ticks` is one value per supplied contract, in ticks, positive when
    that contract month historically prints ABOVE the smooth curve. Spot VIX is
    not adjusted -- it is a 30-day index, not a contract month.
    """
    off = np.asarray(offsets_ticks, dtype=float) / 100.0
    p = np.asarray(prices, dtype=float)
    if off.shape != p.shape or np.isnan(off).any():
        return None, None
    fitted, resid = fit_curve(dtes, p - off, spot_vix, basis, spot_weight)
    if fitted is None:
        return None, None
    # Report the fitted level back on the unadjusted price scale so it can be
    # drawn against the live curve; the residual stays seasonally adjusted.
    return fitted + off, resid


def structure_deviation(weights, legs, residuals: dict[int, float]) -> float | None:
    """
    Weighted sum of leg residuals.

    Scale-preserving: residuals in ticks give a deviation in ticks. Both
    `live_residuals` and the history panel work in ticks, so no rescaling
    happens here -- doing it silently is how a factor of 100 gets in.
    """
    try:
        return sum(w * residuals[g] for w, g in zip(weights, legs))
    except (KeyError, TypeError):
        return None


def static_roll_carry(dtes, prices, spot_vix, weights, legs) -> float | None:
    """
    What the structure earns in one session if the curve SHAPE holds and only
    the contracts age. Interpolates the observed curve (with spot pinned at
    dte=0) one trading day closer to expiry.

    This is the multi-leg generalisation of the front-month daily roll, and it
    is what tells you whether a fly is being paid or is paying to wait.
    """
    d = np.asarray(dtes, dtype=float)
    p = np.asarray(prices, dtype=float)
    if np.isnan(p).any() or np.isnan(d).any():
        return None
    knots_d = np.concatenate([[0.0], d])
    knots_p = np.concatenate([[float(spot_vix)], p]) if spot_vix is not None else p
    if spot_vix is None:
        knots_d = d
    order = np.argsort(knots_d)
    knots_d, knots_p = knots_d[order], knots_p[order]

    idx = {g: i for i, g in enumerate(sorted({*legs}))}
    # map generic -> position in the supplied arrays (they arrive G1..Gn ordered)
    total = 0.0
    for w, g in zip(weights, legs):
        i = g - 1
        if i >= len(d):
            return None
        new = float(np.interp(max(d[i] - 1.0, 0.0), knots_d, knots_p))
        total += w * (new - p[i])
    return total * 100.0
