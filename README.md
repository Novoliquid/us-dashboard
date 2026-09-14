# US Market Dashboard

Static daily dashboard: US indices, commodities, FX, bonds, crypto, and all S&P 500
constituents grouped by GICS sector. Updated after the US close by a scheduled job.

- `constituents.py` — S&P 500 list + sector from Wikipedia, shares outstanding from Yahoo (weekly)
- `fetch.py` — closes / day % / 1M % for everything → `data/latest.json` (+ `data/history/`)
- `index.html` — renders `data/latest.json`; no build step
- `run.sh` — fetch → commit → push; prints a one-line summary

Data: Yahoo Finance (via yfinance), Japan MOF for JGB yields. Personal use.

Definitions: day = close / previous close − 1 (bonds: bp); 1M = close vs. close one calendar
month earlier (last trading day on or before). Red = up, blue = down (Korean convention).
