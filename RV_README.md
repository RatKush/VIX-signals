# VIX RV Signal Board

Opportunistic relative-value signals on VX term-structure packages — spreads,
flies, 1:2 and 2:3 ratios. Finds structures priced away from the fitted curve,
sizes a target and stop from each structure's own volatility, and says whether
history supports taking it.

This runs alongside the existing `server.py` (port 5002) and does not touch it.

## Run

```bash
python rv_server.py                  # port 5003
python rv_server.py --port 5010 --workbook vix_live.xlsx
python rv_server.py --rebuild        # force a fresh history build
python rv_server.py --price-field Settle
python rv_server.py --no-excel       # read the saved file, not the open workbook
```

Leave `vix_live.xlsx` open in Excel with RTD running. Saving it is not required.

First start downloads the Cboe cache (~166 contract files + 7 index files) and
builds the history panel. That takes under a minute cold, ~10s warm. Afterwards
the live curve refreshes every 15 seconds from Excel's memory and history
rebuilds once every 12 hours. The board polls every 5 seconds, so a tick reaches
the plots within a few seconds of being pulled.

## Data

| What | Source | Notes |
|---|---|---|
| Per-contract VX history | `cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_<expiry>.csv` | OHLC, settle, volume, OI. Free, from Jan 2013 |
| Index history | `cdn.cboe.com/api/global/us_indices/daily_prices/<IDX>_History.csv` | VIX, VIX1D, VIX9D, VIX3M, VIX6M, VVIX, SKEW |
| Today's curve | open `vix_live.xlsx` → `RTD` sheet, read live via xlwings | column A ticker, B Last, C Settle. **Read out of Excel's memory, not the file** |
| Spot VIX anchor | Yahoo `^VIX` intraday, Cboe close as fallback | needed for the curve fit |
| Expiry calendar | computed | Wednesday 30 days before the following month's third Friday |

No Bloomberg dependency for history. The workbook supplies only today's live
prices.

**The workbook never has to be saved.** RTD formulas hold their value in Excel's
memory and only reach the file on save, so the board attaches to the already-open
workbook with xlwings and reads the cells at their current value, every
15 seconds. Nothing is written back — the read is strictly read-only.

If Excel is not running, it falls back to `openpyxl` on the file, which returns
whatever Excel cached at its **last save**, and the board says so in the header
(amber `saved file HH:MM (N min old)` instead of green `RTD live`). That
distinction is deliberate: a fallback that looked identical to the live path is
how stale prices get traded. Run with `--no-excel` to force the file path.

## The signal

1. **Stage one — measure the seasonality.** Fit `price ≈ a + b·√dte + c·dte`
   through the generics plus spot VIX pinned at dte = 0 at double weight, and
   take each contract's residual. Pooled by the contract's own calendar month,
   those residuals are not noise:

   | | Oct | Feb | Jan | Apr | Sep | Jul | Nov | Mar | May | Aug | Jun | **Dec** |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|
   | median, ticks | +13.0 | +9.0 | +7.3 | +7.1 | +4.9 | +3.1 | +0.8 | +0.4 | −2.6 | −6.1 | −6.7 | **−31.9** |
   | % negative | 18 | 26 | 30 | 30 | 34 | 42 | 47 | 49 | 59 | 73 | 72 | **98** |

   December has printed below a plain smooth fit in **98% of all sessions and
   in every single year** of the sample. That is a property of the contract, not
   a mispricing. October is the mirror image, carrying the crash-season premium.

2. **Stage two — remove it, then refit.** Subtract each contract's month offset
   from its price and fit the smooth curve again. Doing it *before* the fit
   matters twice: the offending contract stops reporting a residual it always
   has, and it stops dragging the fitted curve down around its own tenor, which
   was making its neighbours look rich.

   Offsets are estimated **causally** — the median of residuals for that
   contract month seen strictly earlier — so nothing in the backtest sees its
   own future. Structures touching a month with fewer than 24 prior
   observations are left unscored rather than scored against a guess.

3. A structure's **deviation** is the weighted sum of its legs' *adjusted*
   residuals — which is what a fly or ratio measures by construction.

4. **Trigger**: a single rolling-252 z-score of that adjusted deviation,
   `|z| ≥ 1.5`, traded against it (sell rich, buy cheap). The same-anchor-month
   z-score is shown for transparency but gates nothing — once the seasonality
   is removed at contract level, confirming against a month baseline corrects
   for it twice, and doing so *reduced* total edge (+1736 against +1788 ticks a
   year) while costing a third of the opportunities.

5. **Bracket**: target 2σ, stop 1σ of the structure's own trailing 60-day daily
   move, 15-session time stop. Fixed tick targets underperform because σ ranges
   from 10 to 92 ticks across the curve.

6. **Overlap collapse**: fired signals sharing two or more legs on the same side
   are one view in different clothes — a fly, a 1:2 and a 2:3 on the same three
   generics move together. They are grouped, the highest-expectancy expression
   is marked `preferred`, and the rest are demoted to `DUPLICATE` so the board
   cannot be read as independent opportunities.

Removing the seasonality lifted total incremental edge from **+1,415 to +1,791
ticks a year** and improved 16 of 18 signal structures.

## Reading the board

Two chart forms, both showing the same thing at different levels.

**Live curve against the fit** — the live board as a solid line, the seasonally
adjusted fit dashed. Each contract's month offset is carried back into the
fitted line, so the vertical gap is that contract's adjusted residual.

**Structure fits** — one panel per family (adjacent calendar, fly, 1:2, 2:3),
each a ladder across the curve: `G1G2G3`, `G2G3G4`, and so on. Solid is where
the structure is actually trading, dashed is what the fitted curve implies.
Because the fitted level already carries the offsets back, `actual − fitted`
**is** the adjusted deviation exactly — the gap on the chart is the signal, so
there is no separate residual panel. The shaded band is the ±1.5σ trigger; a
filled marker with a labelled number means it fired.

Only consecutive-leg structures appear here, because a family with mixed leg
spacing (wide calendars, skip-flies) would imply a sequence along the x-axis
that does not exist. Those sit in the carry table.

## Verdicts

| Verdict | Meaning |
|---|---|
| `TRADE` | fired on the adjusted fit, structure calibrated positive |
| `HALF SIZE` | month offsets not yet estimated, so it is scored on the plain fit |
| `DUPLICATE` | same view as another fired structure sharing its legs |
| `VETO` | this anchor month's past firings did not reach the bracket |
| `WATCH` | `1.0 ≤ |z| < 1.5` |
| `NO EDGE` | calibrated expectancy or incremental edge not positive |
| `NOT TRADED` | carry vehicle — its deviation signal has no incremental edge |
| `FLAT` | nothing doing |

The seasonal veto is measured, not hard-coded: it fires when the anchor month's
median favourable excursion is below 55% of the structure's historical target
(min 8 observations). It is now a **backstop** rather than the main defence —
the contract-level offsets remove the usual seasonal false signals before they
ever reach the trigger. December's G3G4G5 structures, which read +2.8σ on a
plain fit, come in around +1.3σ once adjusted and simply do not fire.

## Units

Everything is in **ticks** — 0.01 index points of the weighted sum, which is
**$10 on one structure unit** at the VX $1,000 multiplier. Same 100× convention
as `phase2_structure_engine`.

Costs are real: **$2.50 per contract per round trip**, so $5 / $10 / $15 / $25
for a spread / fly / 1:2 / 2:3. Calibrated expectancies add one further tick of
slippage.

## Which structures do what

Signal-traded (flies and ratios — carry-neutral, so the deviation is visible):
`FLY_*`, `RATIO_12_*`, `RATIO_23_*`.

Carry-traded, **not** signal-traded (`SPREAD_*`, `SKIPFLY_*`): these have a
large carry drift that buries their reversion, and the deviation signal
measurably *subtracts* from simply selling and rolling them. They appear on the
board for context with `NOT TRADED`.

Sign conventions match the existing engine: `SPREAD_GiGj = Gi − Gj` (negative in
contango), `FLY = [+1,−2,+1]`, `RATIO_12 = [+1,−3,+2]`, `RATIO_23 = [+2,−5,+3]`.

## Library use

```python
from vix_rv import CboeCache, load_or_build, read_live, fetch_spot_vix, build_board

cache = CboeCache("cache")
cache.sync_contracts(); cache.sync_indices()
hist = load_or_build(cache)

spot, src = fetch_spot_vix()
curve = read_live("vix_live.xlsx", spot_vix=spot, spot_source=src)
board = build_board(hist, curve)

print(board.loc[board.verdict.eq("TRADE"),
                ["structure","side","z_roll","z_month","target_ticks","stop_ticks"]])
print(hist.calibration.sort_values("edge_ticks", ascending=False).head(10))
```

`hist.calibration` carries the per-structure historical record: trades per year,
hit rate, stop rate, expectancy per trade, annual ticks, the always-sell
baseline, and the incremental edge over it. `vix_rv.history.simulate()` reruns
any structure with different thresholds or brackets.

## Endpoints

`/api/board` · `/api/curve` · `/api/calibration` · `/api/seasonality` ·
`/api/status` (GET) and `/api/refresh` · `/api/rebuild` (**POST** — they mutate
state and block, so they are not reachable by a stray GET).

`/api/board` reports `source_kind` (`excel` or `file`), `source_note`, and
`refresh_seconds`, so a client can always tell live data from saved data.

`hist.month_offset` is the live offset table, `hist.month_effect` the raw month
diagnostic, and `vix_rv.seasonal.summary()` regenerates it from any residual
panel.

## Honest limits

- Barriers are checked on **settlements**, so an overnight gap can carry an exit
  well past the stop — as it would in practice, but intraday paths are invisible.
- Expectancies come from one 13.6-year sample with thresholds and bracket chosen
  by search. The variance-ratio evidence underneath (every structure's deviation
  reverts, VR₅ 0.41–0.94) is parameter-free; the per-trade numbers are
  optimistically biased. Paper a quarter at one unit before sizing.
- 2013–2017 was materially weaker than 2018 onward for these signals.
- A 2:1 target/stop bracket generates some positive expectancy on any
  mean-reverting series regardless of direction — about 21% of the headline.
  Judge structures on `edge_ticks`, never on `ann_ticks`.
