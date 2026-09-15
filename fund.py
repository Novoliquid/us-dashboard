"""Weekly fundamentals pull -> data/fund/<TICKER>.json (one file per S&P 500 stock)

Source: Yahoo Finance (yfinance). Per ticker: valuation/profitability ratios (info),
last 5 quarterly + 4 annual income statements, analyst EPS/revenue consensus
(current quarter, current FY, next FY) and recent earnings dates with surprise.

Usage: python fund.py [TICKER ...]   (default: all tickers in data/sp500.json)
Exit 0 = ok (>= 90% of tickers written), 2 = too many failures.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).parent
FUND = ROOT / "data" / "fund"
KST = ZoneInfo("Asia/Seoul")
WORKERS = 4

RATIO_KEYS = {
    "pe": "trailingPE", "fpe": "forwardPE", "pb": "priceToBook", "ps": "priceToSalesTrailing12Months",
    "ev_ebitda": "enterpriseToEbitda", "peg": "pegRatio",
    "roe": "returnOnEquity", "roa": "returnOnAssets", "gm": "grossMargins", "om": "operatingMargins",
    "nm": "profitMargins", "rev_growth": "revenueGrowth", "eps_growth": "earningsGrowth",
    "div_yield": "dividendYield", "payout": "payoutRatio", "beta": "beta", "de": "debtToEquity",
    "cur_ratio": "currentRatio", "hi52": "fiftyTwoWeekHigh", "lo52": "fiftyTwoWeekLow",
    "eps_ttm": "trailingEps", "eps_fwd": "forwardEps", "target": "targetMeanPrice",
    "rec": "recommendationKey", "analysts": "numberOfAnalystOpinions", "mcap": "marketCap",
    "fcf": "freeCashflow", "ebitda": "ebitda", "rev_ttm": "totalRevenue",
}
STMT_ROWS = {"rev": "Total Revenue", "op": "Operating Income", "ni": "Net Income", "eps": "Diluted EPS",
             "gp": "Gross Profit", "ebitda": "EBITDA"}


def num(x):
    try:
        if x is None or pd.isna(x):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def stmt_rows(df: pd.DataFrame | None, n: int) -> list[dict]:
    if df is None or df.empty:
        return []
    out = []
    for col in list(df.columns)[:n]:  # newest first
        row = {"period": pd.Timestamp(col).strftime("%Y-%m-%d")}
        for k, label in STMT_ROWS.items():
            row[k] = num(df.loc[label, col]) if label in df.index else None
        out.append(row)
    return out


def estimates(t: yf.Ticker) -> dict:
    out = {}
    for key, getter in (("eps", "earnings_estimate"), ("rev", "revenue_estimate")):
        try:
            df = getattr(t, getter)
        except Exception:
            df = None
        out[key] = {}
        if df is None or df.empty:
            continue
        for per in ("0q", "+1q", "0y", "+1y"):
            if per in df.index:
                r = df.loc[per]
                out[key][per] = {"avg": num(r.get("avg")), "low": num(r.get("low")), "high": num(r.get("high")),
                                 "n": num(r.get("numberOfAnalysts")), "growth": num(r.get("growth")),
                                 "year_ago": num(r.get("yearAgoEps", r.get("yearAgoRevenue")))}
    return out


def earnings_dates(t: yf.Ticker) -> list[dict]:
    try:
        df = t.get_earnings_dates(limit=8)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    rows = []
    for idx, r in df.iterrows():
        rows.append({"date": pd.Timestamp(idx).strftime("%Y-%m-%d"), "est": num(r.get("EPS Estimate")),
                     "actual": num(r.get("Reported EPS")), "surprise": num(r.get("Surprise(%)"))})
    return rows


def pull(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info or {}
    ratios = {k: info.get(src) for k, src in RATIO_KEYS.items()}
    ratios = {k: (v if isinstance(v, str) else num(v)) for k, v in ratios.items()}
    annual = stmt_rows(t.income_stmt, 4)
    fy_end = annual[0]["period"] if annual else None
    return {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName"),
        "updated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "profile": {"sector": info.get("sector"), "industry": info.get("industry"), "website": info.get("website"),
                    "summary": (info.get("longBusinessSummary") or "")[:600], "employees": info.get("fullTimeEmployees"),
                    "currency": info.get("financialCurrency") or info.get("currency")},
        "ratios": ratios,
        "quarterly": stmt_rows(t.quarterly_income_stmt, 5),
        "annual": annual,
        "fy_end": fy_end,  # last reported fiscal year end -> current FY = year(fy_end)+1
        "estimates": estimates(t),
        "earnings_dates": earnings_dates(t),
    }


def pull_retry(ticker: str) -> tuple[str, dict | None, str | None]:
    for attempt in range(3):
        try:
            d = pull(ticker)
            if not d["name"] and not d["quarterly"]:
                raise RuntimeError("empty response")
            return ticker, d, None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            time.sleep(3 * (attempt + 1))
    return ticker, None, err


def main(argv: list[str]) -> int:
    if argv:
        tickers = argv
    else:
        tickers = [r["ticker"] for r in json.loads((ROOT / "data" / "sp500.json").read_text())["rows"]]
    FUND.mkdir(parents=True, exist_ok=True)
    ok, failed = 0, []
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (ticker, d, err) in enumerate(ex.map(pull_retry, tickers), 1):
            if d is None:
                failed.append(f"{ticker} ({err})")
            else:
                (FUND / f"{ticker}.json").write_text(json.dumps(d, ensure_ascii=False))
                ok += 1
            if i % 50 == 0:
                print(f"{i}/{len(tickers)} ok={ok} failed={len(failed)} {time.time()-t0:.0f}s", flush=True)
    print(f"DONE ok={ok} failed={len(failed)} in {time.time()-t0:.0f}s")
    if failed:
        print("failed: " + ", ".join(failed))
    return 0 if ok >= 0.9 * len(tickers) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
