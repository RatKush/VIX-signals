"""
vix_rv -- relative-value signal engine for VX term-structure packages.

Data spine is Cboe's own free files (per-contract settlement history back to
2013 plus the whole index family); today's board comes from the RTD sheet in
vix_live.xlsx. Nothing here needs a vendor terminal or an open Excel session,
though it will use the live sheet when one is present.

Typical use:

    from vix_rv import CboeCache, load_or_build, read_live, fetch_spot_vix, build_board

    cache = CboeCache("cache")
    cache.sync_contracts(); cache.sync_indices()
    hist = load_or_build(cache)
    spot, src = fetch_spot_vix()
    curve = read_live("vix_live.xlsx", spot_vix=spot, spot_source=src)
    board = build_board(hist, curve)
"""

from .cboe import CboeCache
from .curve import fit_curve, static_roll_carry, structure_deviation
from .history import History, build, load_or_build, simulate
from .live import LiveCurve, fetch_spot_vix, read_live
from .signals import build_board, regime_panel
from .structures import STRUCTURES, CARRY_SET, SIGNAL_SET, Structure

__all__ = [
    "CboeCache", "History", "LiveCurve", "STRUCTURES", "SIGNAL_SET", "CARRY_SET",
    "Structure", "build", "build_board", "fetch_spot_vix", "fit_curve",
    "load_or_build", "read_live", "regime_panel", "simulate",
    "static_roll_carry", "structure_deviation",
]
