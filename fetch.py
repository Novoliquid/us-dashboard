"""Daily data pull -> data/latest.json (+ data/history/YYYY-MM-DD.json)

Sources: Yahoo Finance (yfinance) for everything except JGB yields (Japan MOF CSV).
Also writes data/ohlc/<symbol>.json (1Y daily OHLCV) for every Yahoo symbol, used by stock.html.
Definitions:
  close   = last completed daily bar
  day     = close / prev close - 1          (bonds: yield diff in bp)
  m1/m3   = close / close on (asof - 1/3 calendar months, or the last trading day before) - 1
  ytd     = close / last close of the previous calendar year - 1 (bonds: bp)
  post    = after-hours price / % vs close (stocks only, same session as asof)
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent
DATA = ROOT / "data"
HIST = DATA / "history"
OHLC = DATA / "ohlc"
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


def months_ago(d: pd.Timestamp, n: int) -> pd.Timestamp:
    """Same day-of-month n months earlier, clamped to month end."""
    y, m = d.year, d.month - n
    while m < 1:
        y, m = y - 1, m + 12
    last = (pd.Timestamp(year=y, month=m, day=1) + pd.offsets.MonthEnd(0)).day
    return pd.Timestamp(year=y, month=m, day=min(d.day, last))


def stats(s: pd.Series, kind: str) -> dict | None:
    """s: Close series indexed by date (tz-naive). Returns close/day/m1/asof."""
    s = s.dropna()
    if len(s) < 2:
        return None
    asof = s.index[-1]
    close, prev = float(s.iloc[-1]), float(s.iloc[-2])

    def base(n: int):
        ref = s[s.index <= months_ago(asof, n)]
        return float(ref.iloc[-1]) if len(ref) else None

    def chg(a: float, b):
        if b is None:
            return None
        return round((a - b) * 100 if kind == "yld" else (a / b - 1) * 100, 2)  # yld -> bp

    prev_ye = s[s.index < pd.Timestamp(year=asof.year, month=1, day=1)]  # last close of previous year
    return {
        "close": close,
        "day": chg(close, prev),
        "m1": chg(close, base(1)),
        "m3": chg(close, base(3)),
        "ytd": chg(close, float(prev_ye.iloc[-1]) if len(prev_ye) else None),
        "asof": asof.strftime("%Y-%m-%d"),
    }


def _download(symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
    df = yf.download(
        symbols, period=period, interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )
    out = {}
    for sym in symbols:
        try:
            d = df[sym] if len(symbols) > 1 else df
        except KeyError:
            continue
        d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if len(d):
            d.index = pd.to_datetime(d.index).tz_localize(None)
            out[sym] = d
    return out


def yahoo_ohlc(symbols: list[str], period: str = "13mo", retries: int = 3) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per symbol (tz-naive index, NaN closes dropped).
    Yahoo batch downloads drop symbols at random ("possibly delisted"); retry the leftovers a few times."""
    out = _download(symbols, period)
    for attempt in range(retries):
        missing = [s for s in symbols if s not in out]
        if not missing:
            break
        time.sleep(5 * (attempt + 1))
        print(f"retry {attempt + 1}: {len(missing)} symbols", file=sys.stderr)
        out.update(_download(missing, period))
    return out


def slug(symbol: str) -> str:
    """File-safe symbol: ^GSPC -> _GSPC, DX-Y.NYB -> DX-Y_NYB, GC=F -> GC_F."""
    return "".join(c if c.isalnum() or c == "-" else "_" for c in symbol)


def write_ohlc(symbol: str, d: pd.DataFrame) -> None:
    """Last ~1Y of daily bars for the detail page: data/ohlc/<slug>.json."""
    d = d[d.index >= d.index[-1] - pd.DateOffset(years=1)]
    bars = [[i.strftime("%Y-%m-%d"), round(float(r.Open), 4), round(float(r.High), 4), round(float(r.Low), 4),
             round(float(r.Close), 4), int(r.Volume) if pd.notna(r.Volume) else 0] for i, r in d.iterrows()]
    OHLC.mkdir(parents=True, exist_ok=True)
    (OHLC / f"{slug(symbol)}.json").write_text(json.dumps(
        {"symbol": symbol, "asof": bars[-1][0], "cols": ["date", "o", "h", "l", "c", "v"], "bars": bars}))


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
    ohlc = yahoo_ohlc(y_syms)
    for sym, d in ohlc.items():
        write_ohlc(sym, d)
    closes = {sym: d["Close"] for sym, d in ohlc.items()}
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
            result[grp].append({"name": label, "symbol": sym, "kind": kind, "chart": sym in closes and not sym.startswith("JGB:"), **st})
    return result, missing


def yahoo_quotes(symbols: list[str]) -> dict[str, dict]:
    """Batch quote endpoint: after-hours price/% and its timestamp."""
    from yfinance.data import YfData
    data = YfData()
    fields = "regularMarketPrice,postMarketPrice,postMarketChangePercent,postMarketTime,marketState"
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i : i + 100]
        try:
            r = data.get_raw_json(
                "https://query2.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ",".join(chunk), "fields": fields},
            )
            for q in r["quoteResponse"]["result"]:
                out[q["symbol"]] = q
        except Exception as e:
            print(f"quote chunk {i} failed: {e}", file=sys.stderr)
    return out


def build_stocks() -> tuple[list[dict], list[str], str]:
    meta = json.loads((DATA / "sp500.json").read_text())["rows"]
    tickers = [r["ticker"] for r in meta]
    ohlc = yahoo_ohlc(tickers)
    for sym, d in ohlc.items():
        write_ohlc(sym, d)
    closes = {sym: d["Close"] for sym, d in ohlc.items()}
    quotes = yahoo_quotes(tickers)
    rows, missing = [], []
    for r in meta:
        s = closes.get(r["ticker"])
        st = stats(s, "px") if s is not None else None
        if st is None:
            missing.append(r["ticker"])
            continue
        mcap = st["close"] * r["shares"] if r.get("shares") else None
        q = quotes.get(r["ticker"], {})
        post, post_chg, post_time = None, None, None
        if q.get("postMarketPrice") and q.get("postMarketTime"):
            t = datetime.fromtimestamp(q["postMarketTime"], tz=ZoneInfo("America/New_York"))
            if t.strftime("%Y-%m-%d") == st["asof"]:  # same session as the close
                post = float(q["postMarketPrice"])
                post_chg = round(float(q.get("postMarketChangePercent") or (post / st["close"] - 1) * 100), 2)
                post_time = t.strftime("%H:%M")
        rows.append({**{k: r[k] for k in ("ticker", "name", "sector")}, **st,
                     "post": post, "post_chg": post_chg, "post_time": post_time, "mcap": mcap})
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
            "m3": "same as m1 with 3 calendar months",
            "ytd": "close / last close of previous calendar year - 1 (bonds: bp)",
            "post": "after-hours price and % vs close, same session as asof; time in ET",
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
    n_post = sum(1 for x in stocks if x["post"] is not None)
    print(f"OK asof={asof} stocks={len(stocks)} post={n_post} missing_stocks={s_missing} updated={payload['updated_kst']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
