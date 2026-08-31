"""
VIX relative-value signal board -- Flask server.

    python rv_server.py                       # port 5003
    python rv_server.py --port 5010 --workbook vix_live.xlsx
    python rv_server.py --rebuild             # force a fresh history build

Startup: sync the Cboe cache, build (or load) the history panel and its
per-structure calibration, then read today's curve from the live workbook.

The curve is read out of the OPEN workbook's memory via xlwings every
LIVE_REFRESH_SECONDS, so RTD ticks reach the plots without the sheet ever being
saved. If Excel is not running it falls back to the last-saved file and says so
on the board. History is rebuilt once a day.

Routes
    /                  the board
    /api/board         signal board + regime + curve, as JSON
    /api/curve         live curve with fitted values and residuals
    /api/calibration   full per-structure historical calibration
    /api/refresh       re-read the live workbook now (POST)
    /api/rebuild       force a history rebuild, ~10s (POST)
    /api/status        loader state
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request

from vix_rv import CboeCache, build_board, fetch_spot_vix, load_or_build, read_live
from vix_rv.signals import display_name, live_residuals, regime_panel
from vix_rv.structures import STRUCTURES

app = Flask(__name__)
# Jinja compiles a template once and caches it, and with debug=False Flask
# leaves auto_reload off -- so an edited board would keep serving the old markup
# until the process was restarted, which looks exactly like "my change did
# nothing". The board is a single local page; recompiling on mtime costs
# nothing and removes that trap.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

STATE = {
    "ready": False, "error": None, "loading": True,
    "history": None, "curve": None, "board": None, "regime": None,
    "curve_fit": None, "structure_fits": None, "built_at": None, "curve_at": None,
    "workbook": None, "spot": None, "spot_source": None,
    "sync": None, "price_field": "Last", "prefer_excel": True,
}
# LOCK guards reads/writes of STATE and is held only for the assignment.
# REFRESH_LOCK serialises the whole of refresh_curve(): the live loop and any
# number of /api/refresh requests all land in it, and build_board() writes
# module-level scoring state in vix_rv.signals that two concurrent builds would
# interleave, scoring structures against the wrong anchor month.
LOCK = threading.Lock()
REFRESH_LOCK = threading.RLock()
# How often the open workbook's live RTD values are pulled. The board is read
# off Excel's memory, so this is the real update rate of every plot.
LIVE_REFRESH_SECONDS = 15
HISTORY_MAX_AGE_HOURS = 12.0
# Only meaningful on the file fallback. When the values come from Excel's
# memory they are current by construction, so no staleness rule applies.
WORKBOOK_STALE_MINUTES = 15.0


def _jsonify(obj):
    """NaN/NaT-safe JSON."""
    return json.loads(json.dumps(obj, default=_default, allow_nan=False))


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        f = float(o)
        return None if not np.isfinite(f) else f
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, _dt.datetime, _dt.date)):
        return o.isoformat()
    if o is pd.NaT:
        return None
    if isinstance(o, float) and not np.isfinite(o):
        return None
    raise TypeError(f"not serialisable: {type(o)}")


def _clean_records(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    return _jsonify(df.replace({np.nan: None}).to_dict("records"))


def refresh_curve() -> None:
    """
    Re-read the live workbook and rebuild the board off it.

    Serialised on REFRESH_LOCK -- the live loop and every /api/refresh request
    share the scoring state inside build_board(), so only one may be in flight.
    """
    with REFRESH_LOCK:
        _refresh_curve_locked()


def _refresh_curve_locked() -> None:
    hist = STATE["history"]
    if hist is None:
        return
    wb = STATE["workbook"]
    spot, src = fetch_spot_vix()
    try:
        curve = read_live(wb, price_field=STATE["price_field"],
                          spot_vix=spot, spot_source=src,
                          prefer_excel=STATE["prefer_excel"])
    except Exception as exc:
        with LOCK:
            STATE["error"] = f"live workbook: {exc}"
        return

    # One fit, used for both the board and the drawn curve. Deriving the
    # residuals twice is how the chart and the signal quietly drift apart.
    pack = live_residuals(curve, getattr(hist, "month_offset", None))
    plain, adjusted, offsets, fitted = pack

    board = build_board(hist, curve, residuals=pack)
    regime = regime_panel(hist, curve)

    curve_fit = [{
        "generic": g, "label": curve.label.get(g), "ticker": curve.ticker.get(g),
        "dte": curve.dte.get(g), "price": round(curve.prices[g], 4),
        "month": curve.expiry[g].month if curve.expiry.get(g) else None,
        "fitted": None if not fitted else round(fitted.get(g), 4),
        "residual_raw_ticks": None if not plain else round(plain.get(g), 1),
        "offset_ticks": None if not offsets else round(offsets.get(g), 1),
        "residual_ticks": None if not adjusted else round(adjusted.get(g), 1),
    } for g in curve.generics]

    fits = _structure_fits(curve, board, fitted)

    with LOCK:
        STATE.update(curve=curve, board=board, regime=regime, curve_fit=curve_fit,
                     structure_fits=fits, curve_at=_dt.datetime.now(),
                     spot=spot, spot_source=src,
                     error=None, ready=True, loading=False)


def _structure_fits(curve, board, fitted):
    """
    The same actual-against-fitted picture as the curve chart, but per structure
    family.

    Because the reported fitted level already carries each contract's month
    offset back, the gap between a structure's actual value and its fitted value
    IS its seasonally adjusted deviation -- the signal is the vertical distance
    between the two lines, with no separate residual panel needed.
    """
    if not fitted or board is None or board.empty:
        return None

    def nz(v):
        """Board cells arrive as NaN rather than None in object columns."""
        if v is None or v is pd.NaT:
            return None
        if isinstance(v, float) and not np.isfinite(v):
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v

    by_name = board.set_index("structure")
    out: dict[str, list] = {}
    for name, st in STRUCTURES.items():
        if any(g not in curve.prices for g in st.legs):
            continue
        if any(g not in fitted for g in st.legs):
            continue
        # Only consecutive-leg structures form a ladder that reads left to right.
        # Wide calendars and skip-flies share a family with different spacings,
        # so plotting them on one axis would imply a sequence that isn't there;
        # they live in the carry table instead.
        if list(st.legs) != list(range(st.legs[0], st.legs[0] + len(st.legs))):
            continue
        actual = sum(w * curve.prices[g] for w, g in zip(st.weights, st.legs)) * 100.0
        fit = sum(w * fitted[g] for w, g in zip(st.weights, st.legs)) * 100.0
        row = by_name.loc[name] if name in by_name.index else None
        legs_lbl = "".join(f"G{g}" for g in st.legs)
        out.setdefault(st.family, []).append({
            "structure": name,
            "display": display_name(st, curve),
            # the x-axis reads as months; the family is already the panel title
            "front_month": (curve.label.get(st.legs[0]) or "?").split()[0],
            "legs_label": legs_lbl,
            "front": st.legs[0],
            "months": " / ".join(curve.label.get(g, "?") for g in st.legs),
            "actual": round(actual, 1),
            "fitted": round(fit, 1),
            "deviation": round(actual - fit, 1),
            "z": None if row is None else nz(row["z_roll"]),
            "verdict": None if row is None else nz(row["verdict"]),
            "side": None if row is None else nz(row["side"]),
            "target": None if row is None else nz(row["target_ticks"]),
            # the bracket is carried, not re-derived in the browser: stop is
            # STOP_SIGMA, not "half the target", and that only coincides today
            "stop": None if row is None else nz(row["stop_ticks"]),
            "band_mid": None if row is None else nz(row.get("dev_mean_ticks")),
            "band_half": None if row is None else (
                None if nz(row.get("dev_sd_ticks")) is None
                else round(1.5 * float(row["dev_sd_ticks"]), 1)),
            "exp_ticks": None if row is None else nz(row["exp_ticks"]),
            "gross_contracts": st.gross_contracts,
            "signal_traded": st.signal_traded,
        })
    for fam in out:
        out[fam].sort(key=lambda r: r["front"])
    return out


def bootstrap(workbook: str, cache_dir: str, rebuild: bool) -> None:
    with LOCK:
        STATE.update(loading=True, workbook=workbook)
    try:
        cache = CboeCache(cache_dir)
        sync = {"contracts": cache.sync_contracts(), "indices": cache.sync_indices()}
        hist = load_or_build(cache, path=Path(cache_dir) / "history.pkl",
                             max_age_hours=HISTORY_MAX_AGE_HOURS, force=rebuild)
        with LOCK:
            STATE.update(history=hist, built_at=hist.built_at, sync=sync)
        refresh_curve()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        with LOCK:
            STATE.update(error=str(exc), loading=False, ready=False)


def live_loop() -> None:
    while True:
        time.sleep(LIVE_REFRESH_SECONDS)
        try:
            refresh_curve()
        except Exception as exc:
            print(f"  live refresh failed: {exc}")


def daily_loop() -> None:
    while True:
        time.sleep(3600)
        built = STATE.get("built_at")
        if built and (_dt.datetime.now() - built).total_seconds() > HISTORY_MAX_AGE_HOURS * 3600:
            print("  rebuilding history ...")
            bootstrap(STATE["workbook"], app.config["CACHE_DIR"], rebuild=True)


# ------------------------------------------------------------------ routes

@app.route("/")
def index():
    # no-store for the same reason: a cached copy in the browser hides edits
    # just as effectively as a cached copy in Jinja
    resp = app.make_response(render_template("rv_board.html"))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/api/status")
def api_status():
    with LOCK:
        return jsonify({
            "ready": STATE["ready"], "loading": STATE["loading"], "error": STATE["error"],
            "built_at": STATE["built_at"].isoformat() if STATE["built_at"] else None,
            "curve_at": STATE["curve_at"].isoformat() if STATE["curve_at"] else None,
            "workbook": STATE["workbook"], "price_field": STATE["price_field"],
            "prefer_excel": STATE["prefer_excel"],
            "sync": STATE["sync"],
        })


@app.route("/api/board")
def api_board():
    with LOCK:
        board, regime, curve = STATE["board"], STATE["regime"], STATE["curve"]
        curve_fit, hist = STATE["curve_fit"], STATE["history"]
        fits = STATE["structure_fits"]
        curve_at, built_at = STATE["curve_at"], STATE["built_at"]
    if board is None:
        return jsonify({"ready": False, "error": STATE["error"]}), 503

    signal = board[board["signal_traded"]]
    carry = board[board["carry_traded"]]
    counts = board["verdict"].value_counts().to_dict()
    return jsonify(_jsonify({
        "ready": True,
        "as_of": curve_at.isoformat() if curve_at else None,
        "history_built": built_at.isoformat() if built_at else None,
        "workbook_stamp": curve.as_of.isoformat() if curve else None,
        "source_kind": curve.source_kind if curve else None,
        "source_note": curve.source_note if curve else None,
        # a live read is current by definition; only the file fallback ages
        "workbook_age_min": None if (curve is None or curve.source_kind == "excel")
            else round((_dt.datetime.now() - curve.as_of).total_seconds() / 60.0, 1),
        "workbook_stale_after_min": WORKBOOK_STALE_MINUTES,
        "refresh_seconds": LIVE_REFRESH_SECONDS,
        "price_field": curve.price_field if curve else None,
        "stale": curve.stale if curve else [],
        "regime": regime,
        "curve": curve_fit,
        "structure_fits": fits,
        "signal_board": _clean_records(signal),
        "carry_book": _clean_records(carry),
        "verdict_counts": counts,
        "n_history_sessions": int(len(hist.dates)) if hist else None,
        "seasonality": _seasonality_payload(hist),
    }))


def _seasonality_payload(hist):
    """The contract-month offsets the fit removes, plus the raw month effect."""
    if hist is None or not getattr(hist, "month_offset", None):
        return None
    eff = hist.month_effect
    rows = []
    for m in range(1, 13):
        r = eff.loc[m] if m in eff.index else None
        rows.append({
            "month": m,
            "offset_ticks": round(hist.month_offset.get(m), 1) if m in hist.month_offset else None,
            "raw_median": None if r is None else float(r["median"]),
            "raw_mean": None if r is None else float(r["mean"]),
            "pct_negative": None if r is None else float(r["pct_negative"]),
            "n": None if r is None else int(r["n"]),
        })
    return rows


@app.route("/api/seasonality")
def api_seasonality():
    with LOCK:
        hist = STATE["history"]
    if hist is None:
        return jsonify({"error": "not ready"}), 503
    return jsonify(_jsonify({"seasonality": _seasonality_payload(hist),
                             "built_at": hist.built_at.isoformat()}))


@app.route("/api/curve")
def api_curve():
    with LOCK:
        return jsonify(_jsonify({"curve": STATE["curve_fit"],
                                 "regime": STATE["regime"],
                                 "as_of": STATE["curve_at"].isoformat() if STATE["curve_at"] else None}))


@app.route("/api/calibration")
def api_calibration():
    with LOCK:
        hist = STATE["history"]
    if hist is None:
        return jsonify({"error": "not ready"}), 503
    cal = hist.calibration.reset_index()
    return jsonify(_jsonify({"calibration": _clean_records(cal),
                             "month_stats": hist.month_stats,
                             "built_at": hist.built_at.isoformat()}))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    field = request.args.get("price_field")
    if field in ("Last", "Settle"):
        with LOCK:
            STATE["price_field"] = field
    refresh_curve()
    return jsonify({"ok": STATE["error"] is None, "error": STATE["error"],
                    "curve_at": STATE["curve_at"].isoformat() if STATE["curve_at"] else None})


@app.route("/api/rebuild", methods=["POST"])
def api_rebuild():
    if not REFRESH_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "a refresh or rebuild is already running"}), 409
    try:
        bootstrap(STATE["workbook"], app.config["CACHE_DIR"], rebuild=True)
    finally:
        REFRESH_LOCK.release()
    return jsonify({"ok": STATE["error"] is None, "error": STATE["error"],
                    "built_at": STATE["built_at"].isoformat() if STATE["built_at"] else None})


def _quiet_request_log() -> None:
    """
    Silence werkzeug's per-request line.

    The board polls every 15 seconds, so request logging produces hundreds of
    kilobytes an hour of noise. Worse, if stdout is redirected to a file that
    another process already holds open, that write blocks -- and because the log
    line is emitted after the handler returns, the connection is left open and
    the request appears to hang. Errors still surface.
    """
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


def main():
    ap = argparse.ArgumentParser(description="VIX relative-value signal board")
    ap.add_argument("--workbook", default="vix_live.xlsx")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--port", type=int, default=5003)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--price-field", default="Last", choices=["Last", "Settle"])
    ap.add_argument("--no-excel", action="store_true",
                    help="do not attach to the open workbook; read the saved file "
                         "instead (values will only be as fresh as the last save)")
    args = ap.parse_args()

    app.config["CACHE_DIR"] = args.cache
    STATE["price_field"] = args.price_field
    STATE["prefer_excel"] = not args.no_excel
    _quiet_request_log()

    print(f"  workbook   {args.workbook}")
    print(f"  cache      {args.cache}")
    print("  syncing Cboe data and building history ...")
    bootstrap(args.workbook, args.cache, args.rebuild)
    if STATE["error"]:
        print(f"  ERROR: {STATE['error']}")
    else:
        b = STATE["board"]
        n = 0 if b is None else int((b["verdict"] == "TRADE").sum())
        print(f"  history {len(STATE['history'].dates)} sessions, "
              f"{len(STATE['history'].calibration)} structures calibrated")
        c = STATE["curve"]
        print(f"  live curve {len(c.generics)} generics, "
              f"spot {STATE['spot']} ({STATE['spot_source']}), {n} live TRADE signals")
        print(f"  prices     {c.source_kind} -- {c.source_note}")
        if c.source_kind != "excel":
            print("  NOTE: not attached to Excel, so prices are only as fresh "
                  "as the last save of the workbook")
        print(f"  refresh    every {LIVE_REFRESH_SECONDS}s from Excel's memory")

    threading.Thread(target=live_loop, daemon=True).start()
    threading.Thread(target=daily_loop, daemon=True).start()
    print(f"\n  http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
