# US Market Dashboard

Static daily dashboard: US indices, commodities, FX, bonds, crypto, and all S&P 500
constituents grouped by GICS sector. Updated after the US close by a scheduled job.

- `constituents.py` — S&P 500 list + sector from Wikipedia, shares outstanding from Yahoo (weekly)
- `fetch.py` — closes / day % / 1M % for everything → `data/latest.json` (+ `data/history/`),
  plus 3Y daily OHLCV per symbol → `data/ohlc/<symbol>.json` (Fear & Greed as `type: line`)
- `fund.py` — per-stock fundamentals (ratios, 5 quarterly + 4 annual income statements, analyst
  EPS/revenue consensus, earnings dates) → `data/fund/<TICKER>.json` (weekly)
- `index.html` — renders `data/latest.json`; no build step
- `stock.html?t=SYM` — detail page: 3M–3Y candlestick (lightweight-charts CDN), ratios, last 4 quarters,
  3 fiscal years, current/next FY consensus. Indices/macro symbols get the chart only.
- `run.sh` — daily: fetch → commit → push; prints a one-line summary
- `fund.sh` — weekly: fund.py → commit → push

Data: Yahoo Finance (via yfinance), Japan MOF for JGB yields. Personal use.

Definitions: day = close / previous close − 1 (bonds: bp); 1M = close vs. close one calendar
month earlier (last trading day on or before). Red = up, blue = down (Korean convention).
