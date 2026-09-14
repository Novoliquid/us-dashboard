"""Daily data pull -> data/latest.json (+ data/history/YYYY-MM-DD.json)

Sources: Yahoo Finance (yfinance) for everything except JGB yields (Japan MOF CSV).
Definitions:
  close   = last completed daily bar
  day     = close / prev close - 1          (bonds: yield diff in bp)
  m1      = close / close on (asof - 1 calendar month, or the last trading day before) - 1
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent
DATA = ROOT / "data"
HIST = DATA / "history"
KST = ZoneInfo("Asia/Seoul")

# label, yahoo symbol, kind ("px" price, "yld" yield in %)
MACRO = {
    "Indices": [
        ("Dow Jones", "^DJI", "px"),
        ("S&P 500", "^GSPC", "px"),
        ("Nasdaq", "^IXIC", "px"),
    ],
    "Commodities": [
        ("Gold", "GC=F", "px"),
        ("Silver", "SI=F", "px"),
        ("Copper", "HG=F", "px"),
        ("WTI", "CL=F", "px"),
        ("Brent", "BZ=F", "px"),
    ],
    "Currencies": [
        ("USD/JPY", "JPY=X", "px"),
        ("USD/KRW", "KRW=X", "px"),
        ("Dollar Index", "DX-Y.NYB", "px"),
    ],
    "Bonds": [
        ("US 10Y", "^TNX", "yld"),
        ("US 30Y", "^TYX", "yld"),
        ("JP 10Y", "JGB:10年", "yld"),
        ("JP 30Y", "JGB:30年", "yld"),
    ],
    "Crypto": [
        ("Bitcoin", "BTC-USD", "px"),
        ("Ethereum", "ETH-USD", "px"),
    ],
}

JGB_CUR = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
JGB_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"  # lags current month
UA = {"User-Agent": "Mozilla/5.0 (us-dashboard; personal use)"}


def month_ago(d: pd.Timestamp) -> pd.Timestamp:
    """Same day-of-month one month earlier, clamped to month end."""
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    last = (pd.Timestamp(year=y, month=m, day=1) + pd.offsets.MonthEnd(0)).day
    return pd.Timestamp(year=y, month=m, day=min(d.day, last))


def stats(s: pd.Series, kind: str) -> dict | None:
    """s: Close series indexed by date (tz-naive). Returns close/day/m1/asof."""
    s = s.dropna()
    if len(s) < 2:
        return None
    asof = s.index[-1]
    close, prev = float(s.iloc[-1]), float(s.iloc[-2])
    ref = s[s.index <= month_ago(asof)]
    m1_base = float(ref.iloc[-1]) if len(ref) else None
    if kind == "yld":
        day = (close - prev) * 100  # bp
        m1 = (close - m1_base) * 100 if m1_base is not None else None
    else:
        day = (close / prev - 1) * 100
        m1 = (close / m1_base - 1) * 100 if m1_base else None
    return {
        "close": close,
        "day": round(day, 2),
        "m1": round(m1, 2) if m1 is not None else None,
        "asof": asof.strftime("%Y-%m-%d"),
    }


def yahoo_closes(symbols: list[str], period: str = "3mo") -> dict[str, pd.Series]:
    df = yf.download(
        symbols, period=period, interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )
    out = {}
    for sym in symbols:
        try:
            s = df[sym]["Close"] if len(symbols) > 1 else df["Close"]
        except KeyError:
            continue
        s = s.dropna()
        if len(s):
            s.index = pd.to_datetime(s.index).tz_localize(None)
            out[sym] = s
    return out


def jgb_series() -> dict[str, pd.Series]:
    """Japan MOF daily yields. Reiwa dates (R8.9.1) -> 2026-09-01."""
    def parse(url: str) -> pd.DataFrame:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        txt = r.content.decode("shift_jis", errors="ignore")
        lines = [l for l in txt.splitlines() if l and l[0] in "RHS" and "." in l.split(",")[0]]
        hdr = next(l for l in txt.splitlines() if l.startswith("基準日")).split(",")
        rows = [l.split(",") for l in lines]
        df = pd.DataFrame(rows, columns=hdr[: len(rows[0])])

        def wareki(s: str):
            era, rest = s[0], s[1:]
            y, m, d = (int(x) for x in rest.split("."))
            base = {"R": 2018, "H": 1988, "S": 1925}[era]
            return pd.Timestamp(year=base + y, month=m, day=d)

        df.index = [wareki(x) for x in df["基準日"]]
        return df.drop(columns=["基準日"])

    frames = []
    for url in (JGB_ALL, JGB_CUR):
        try:
            frames.append(parse(url))
        except Exception as e:
            print(f"JGB {url}: {e}", file=sys.stderr)
    if not frames:
        raise RuntimeError("no JGB data")
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    out = {}
    for col in ("10年", "30年"):
        out[f"JGB:{col}"] = pd.to_numeric(df[col], errors="coerce")
    return out


def build_macro() -> tuple[dict, list[str]]:
    y_syms = [s for grp in MACRO.values() for _, s, _ in grp if not s.startswith("JGB:")]
    closes = yahoo_closes(y_syms)
    try:
        closes.update(jgb_series())
    except Exception as e:
        print(f"JGB fetch failed: {e}", file=sys.stderr)
    result, missing = {}, []
    for grp, items in MACRO.items():
        result[grp] = []
        for label, sym, kind in items:
            st = stats(closes[sym], kind) if sym in closes else None
            if st is None:
                missing.append(sym)
                st = {"close": None, "day": None, "m1": None, "asof": None}
            result[grp].append({"name": label, "symbol": sym, "kind": kind, **st})
    return result, missing


def build_stocks() -> tuple[list[dict], list[str], str]:
    meta = json.loads((DATA / "sp500.json").read_text())["rows"]
    tickers = [r["ticker"] for r in meta]
    closes = yahoo_closes(tickers)
    rows, missing = [], []
    for r in meta:
        s = closes.get(r["ticker"])
        st = stats(s, "px") if s is not None else None
        if st is None:
            missing.append(r["ticker"])
            continue
        mcap = st["close"] * r["shares"] if r.get("shares") else None
        rows.append({**{k: r[k] for k in ("ticker", "name", "sector")}, **st, "mcap": mcap})
    # market asof = most common asof date among stocks
    asof = pd.Series([x["asof"] for x in rows]).mode().iloc[0] if rows else None
    return rows, missing, asof


def main() -> int:
    macro, m_missing = build_macro()
    stocks, s_missing, asof = build_stocks()
    updated = datetime.now(KST)
    payload = {
        "asof": asof,
        "updated_kst": updated.strftime("%Y-%m-%d %H:%M"),
        "macro": macro,
        "stocks": stocks,
        "missing": {"macro": m_missing, "stocks": s_missing},
        "definitions": {
            "day": "close / prev close - 1 (bonds: bp)",
            "m1": "close / close 1 calendar month earlier (last trading day before) - 1 (bonds: bp)",
        },
    }
    ok = asof is not None and len(m_missing) == 0 and len(s_missing) <= 5
    prev_asof = None
    latest = DATA / "latest.json"
    if latest.exists():
        prev_asof = json.loads(latest.read_text()).get("asof")
    if not ok:
        print(f"FAIL asof={asof} macro_missing={m_missing} stocks_missing={len(s_missing)}", file=sys.stderr)
        return 2
    if prev_asof == asof:
        print(f"no new trading day (asof {asof}); refreshing anyway for non-equity items")
    DATA.mkdir(exist_ok=True)
    HIST.mkdir(exist_ok=True)
    latest.write_text(json.dumps(payload, ensure_ascii=False))
    (HIST / f"{asof}.json").write_text(json.dumps(payload, ensure_ascii=False))
    print(f"OK asof={asof} stocks={len(stocks)} missing_stocks={s_missing} updated={payload['updated_kst']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
