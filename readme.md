VIX-DASHBOARD-MAIN/
│
├── templates/
│   └── rv_board.html          the board
│
├── vix_rv/
│   ├── __init__.py
│   ├── calendar_vx.py         VX expiries, trading-day counts
│   ├── cboe.py                Cboe download + local cache
│   ├── curve.py               the smooth fit and its residuals
│   ├── history.py             history panel + per-structure calibration
│   ├── live.py                live curve from the open workbook (xlwings)
│   ├── seasonal.py            contract-month offsets
│   ├── signals.py             the signal board
│   └── structures.py          structure definitions
│
├── cache/                     Cboe CSVs + built history.pkl (regenerates)
├── reqs.txt
├── rv_server.py               the app
├── RV_README.md               full documentation — read this one
└── vix_live.xlsx              live RTD curve, read from Excel's memory

---

## Setup

```bash
python -m venv venv
venv\Scripts\activate           # Windows;  source venv/bin/activate elsewhere
pip install -r reqs.txt
```

## Running

```bash
python rv_server.py                  # port 5003
python rv_server.py --port 5004
python rv_server.py --rebuild        # force a fresh history build
python rv_server.py --no-excel       # read the saved file instead of live Excel
```

Leave `vix_live.xlsx` **open in Excel** with RTD running. It does not need to be
saved — the board attaches to the open workbook and reads the cells at their
current value every 15 seconds. If Excel is not running it falls back to the
last-saved file and says so in the header.

See `RV_README.md` for the method, the verdicts, and the honest limits.
