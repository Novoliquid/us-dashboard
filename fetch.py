"""Daily data pull -> data/latest.json (+ data/history/YYYY-MM-DD.json)

Sources: Yahoo Finance (yfinance) for everything except JGB yields (Japan MOF CSV), US 2Y (FRED DGS2) and
Fear & Greed (CNN).
Also writes data/ohlc/<symbol>.json (3Y daily OHLCV, or a daily line for the non-Yahoo series) for every symbol, used by stock.html.
Definitions:
  close   = last completed daily bar
  day     = close / prev close - 1          (bonds: yield diff in bp; Fear & Greed: point diff)
  m1/m3   = close / close on (asof - 1/3 calendar months, or the last trading day before) - 1
  ytd     = close / last close of the previous calendar year - 1 (bonds: bp)
  post    = after-hours price / % vs close (stocks only, same session as asof)
"""
import io
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
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
        ("Russell 2000", "^RUT", "px"),
        ("SOX (반도체)", "^SOX", "px"),
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
        ("US 2Y", "FRED:DGS2", "yld"),  # Treasury constant maturity via FRED; posted next business day
        ("US 10Y", "^TNX", "yld"),
        ("US 30Y", "^TYX", "yld"),
        ("JP 10Y", "JGB:10年", "yld"),
        ("JP 30Y", "JGB:30年", "yld"),
    ],
    "Crypto": [
    # top 5 by market cap, stablecoins skipped (Tether ranks 3rd but is pegged, so it says nothing on a price board)
        ("Bitcoin", "BTC-USD", "px"),
        ("Ethereum", "ETH-USD", "px"),
        ("BNB", "BNB-USD", "px"),
        ("XRP", "XRP-USD", "px"),
        ("Solana", "SOL-USD", "px"),
    ],
    "Fear & Greed": [  # 0-100 sentiment scores; "fg" kind -> changes in points
        ("Stocks (CNN)", "FNG:STOCK", "fg"),
    ],
}
EXTERNAL = ("JGB:", "FNG:", "FRED:")  # non-Yahoo symbol prefixes
OHLC_PERIOD = "37mo"  # 3Y of bars + buffer for the month-ago lookups
OHLC_YEARS = 3

# Economic calendar: US releases worth watching, as (Nasdaq eventName lowercased, nth row of that name, label, tag).
# Nasdaq repeats the same eventName for MoM then YoY, so the index picks which one.
CAL_WATCH = [
    ("nonfarm payrolls", 0, "비농업 고용", "고용", 1),
    ("unemployment rate", 0, "실업률", "고용", 1),
    ("average hourly earnings", 0, "시간당 임금 MoM", "고용", 1),
    ("adp nonfarm employment change", 0, "ADP 민간고용", "고용", 2),
    ("jolts job openings", 0, "JOLTS 구인", "고용", 2),
    ("cpi", 0, "CPI MoM", "물가", 1),
    ("cpi", 1, "CPI YoY", "물가", 2),
    ("core cpi", 0, "근원 CPI MoM", "물가", 2),
    ("core cpi", 1, "근원 CPI YoY", "물가", 1),
    ("core pce price index", 0, "근원 PCE MoM", "물가", 1),
    ("core pce price index", 1, "근원 PCE YoY", "물가", 1),
    ("pce price index", 0, "PCE MoM", "물가", 2),
    ("ppi", 0, "PPI MoM", "물가", 2),
    ("core ppi", 0, "근원 PPI MoM", "물가", 2),
    ("michigan 1-year inflation expectations", 0, "미시간 1년 기대인플레", "물가", 3),
    ("ism manufacturing pmi", 0, "ISM 제조업", "경기", 3),
    ("ism non-manufacturing pmi", 0, "ISM 서비스업", "경기", 3),
    ("retail sales", 0, "소매판매 MoM", "경기", 3),
]
CAL_DAYS = 31      # how far ahead to look
CAL_MAX = 12       # rows kept on the card; lowest-priority tiers drop first
ERN_DAYS = 40      # earnings look-ahead; the first S&P 500 names can be a couple of weeks out
ERN_MAX = 12
NQ_CAL = "https://api.nasdaq.com/api/calendar/economicevents?date={d}"
NQ_ERN = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
FOMC_CAL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
ET = ZoneInfo("America/New_York")

JGB_CUR = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
JGB_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"  # lags current month
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
FNG_CNN = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"  # ~1Y history; bot-blocked without browser headers
UA = {"User-Agent": "Mozilla/5.0 (us-dashboard; personal use)"}
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json", "Referer": "https://www.cnn.com/markets/fear-and-greed", "Origin": "https://www.cnn.com",
}


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
        if kind == "yld":
            return round((a - b) * 100, 2)  # bp
        if kind == "fg":
            return round(a - b, 1)  # points
        return round((a / b - 1) * 100, 2)

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


def yahoo_ohlc(symbols: list[str], period: str = OHLC_PERIOD, retries: int = 3) -> dict[str, pd.DataFrame]:
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
    """Last ~3Y of daily bars for the detail page: data/ohlc/<slug>.json."""
    d = d[d.index >= d.index[-1] - pd.DateOffset(years=OHLC_YEARS)]
    bars = [[i.strftime("%Y-%m-%d"), round(float(r.Open), 4), round(float(r.High), 4), round(float(r.Low), 4),
             round(float(r.Close), 4), int(r.Volume) if pd.notna(r.Volume) else 0] for i, r in d.iterrows()]
    OHLC.mkdir(parents=True, exist_ok=True)
    (OHLC / f"{slug(symbol)}.json").write_text(json.dumps(
        {"symbol": symbol, "asof": bars[-1][0], "cols": ["date", "o", "h", "l", "c", "v"], "bars": bars}))


def read_line(symbol: str) -> pd.Series | None:
    """Previously written daily line (data/ohlc/<slug>.json, type=line) so short-history sources accumulate over time."""
    f = OHLC / f"{slug(symbol)}.json"
    if not f.exists():
        return None
    try:
        old = json.loads(f.read_text())
        if old.get("type") != "line":
            return None
        return pd.Series({pd.Timestamp(b[0]): float(b[1]) for b in old["bars"]}).sort_index()
    except Exception as e:
        print(f"read_line {symbol}: {e}", file=sys.stderr)
        return None


def write_line(symbol: str, s: pd.Series, dec: int = 3) -> None:
    """Last ~3Y of a daily value series as a line chart file (same folder as OHLC, type=line)."""
    s = s.dropna()
    s = s[s.index >= s.index[-1] - pd.DateOffset(years=OHLC_YEARS)]
    bars = [[i.strftime("%Y-%m-%d"), round(float(v), dec)] for i, v in s.items()]
    OHLC.mkdir(parents=True, exist_ok=True)
    (OHLC / f"{slug(symbol)}.json").write_text(json.dumps(
        {"symbol": symbol, "asof": bars[-1][0], "type": "line", "cols": ["date", "c"], "bars": bars}))


def fng_hist(s: pd.Series) -> dict:
    """Readings shown on the gauge card: previous close, 1 week / 1 month / 1 year ago (last value on or before)."""
    s = s.dropna()
    asof = s.index[-1]

    def at(ts: pd.Timestamp):
        ref = s[s.index <= ts]
        return round(float(ref.iloc[-1]), 1) if len(ref) else None

    return {"prev_close": round(float(s.iloc[-2]), 1) if len(s) > 1 else None,
            "w1": at(asof - pd.Timedelta(days=7)), "m1": at(months_ago(asof, 1)), "y1": at(months_ago(asof, 12))}


def fng_series() -> tuple[dict[str, pd.Series], dict[str, dict]]:
    """Fear & Greed daily scores (0-100) keyed by UTC date, plus per-symbol extras for the gauge card:
    {"rating": source's label for the latest value, "hist": {prev_close, w1, m1, y1}}.
    CNN only serves ~1Y, so it is merged with what we wrote before."""
    series, extra = {}, {}
    try:
        r = requests.get(FNG_CNN, headers=BROWSER, timeout=30)
        r.raise_for_status()
        j = r.json()
        pts = j["fear_and_greed_historical"]["data"]
        s = pd.Series({pd.Timestamp(datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc).date()): float(p["y"]) for p in pts})
        prev = read_line("FNG:STOCK")
        if prev is not None:
            s = pd.concat([prev, s])
        series["FNG:STOCK"] = s[~s.index.duplicated(keep="last")].sort_index()
        fg = j["fear_and_greed"]  # use CNN's own comparison readings so the card matches the site
        extra["FNG:STOCK"] = {"rating": fg["rating"].title(), "hist": {
            "prev_close": round(float(fg["previous_close"]), 1), "w1": round(float(fg["previous_1_week"]), 1),
            "m1": round(float(fg["previous_1_month"]), 1), "y1": round(float(fg["previous_1_year"]), 1)}}
    except Exception as e:
        print(f"FNG CNN failed: {e}", file=sys.stderr)
    return series, extra


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


def fred_series(ids: list[str]) -> dict[str, pd.Series]:
    """FRED daily series (e.g. DGS2 = 2Y Treasury constant maturity, %). '.' marks holidays -> dropped.
    FRED's edge stalls Python requests that send a browser-style User-Agent (never answers); a curl-style UA responds instantly,
    so try that first and fall back to the system curl."""
    import subprocess
    out = {}
    for fid in ids:
        url = FRED_CSV.format(id=fid)
        text = None
        try:
            r = requests.get(url, headers={"User-Agent": "curl/8.7.1"}, timeout=20)
            r.raise_for_status()
            text = r.text
        except Exception as e:
            print(f"FRED {fid} via requests: {e}; trying curl", file=sys.stderr)
            cp = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True, text=True)
            if cp.returncode == 0 and cp.stdout.startswith("observation_date"):
                text = cp.stdout
        if not text:
            raise RuntimeError(f"FRED {fid}: no data")
        df = pd.read_csv(io.StringIO(text))
        s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
        s.index = pd.to_datetime(df.iloc[:, 0])
        out[f"FRED:{fid}"] = s.dropna()
    return out


def _cal_clean(v) -> str | None:
    """Nasdaq writes an empty cell as '&nbsp;' / blank."""
    s = (v or "").replace("&nbsp;", "").strip()
    return s or None


def fomc_events(start, end) -> list[dict]:
    """FOMC decision days and minutes releases from the Fed's own calendar.
    A meeting row reads e.g. 'October 27-28'; minutes land 21 days after the last day (holds for every 2026 meeting so far)."""
    import re
    r = requests.get(FOMC_CAL, headers=UA, timeout=20)
    r.raise_for_status()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
    MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    out = []
    for m in re.finditer(r"(\d{4}) FOMC Meetings(.*?)(?=\d{4} FOMC Meetings|$)", text):
        year, seg = int(m.group(1)), m.group(2)
        if not (start.year <= year <= end.year + 1):
            continue
        for mm in re.finditer(rf"({'|'.join(MON)})(?:/({'|'.join(MON)}))? (\d{{1,2}})-(\d{{1,2}})\*?", seg):
            m1, m2, _d1, d2 = mm.group(1), mm.group(2), int(mm.group(3)), int(mm.group(4))
            last = date(year, MON.index(m2 or m1) + 1, d2)
            for when, label in ((last, "FOMC 회의 (금리 결정)"), (last + timedelta(days=21), "FOMC 의사록")):
                if start <= when <= end:
                    out.append({"date": when.isoformat(), "et": "14:00", "name": label, "tag": "연준", "prio": 0})
    return out


def nasdaq_calendar(start, end) -> list[dict]:
    """US releases with consensus, one API call per day. Consensus is only published a few days ahead, so it is often blank.
    The API is off by one: ?date=D returns the releases of D-1 (checked against weekday-fixed prints -- Dallas Fed Mfg is
    always Monday and lands on ?date=Tuesday, Redbook/JOLTS are Tuesday and land on ?date=Wednesday), so query d+1 for day d.
    Times in the 'gmt' field are actually ET."""
    out = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        try:
            r = requests.get(NQ_CAL.format(d=(d + timedelta(days=1)).isoformat()),
                             headers=BROWSER | {"Referer": "https://www.nasdaq.com/"}, timeout=20)
            rows = ((r.json().get("data") or {}).get("rows")) or []
        except Exception as e:
            print(f"calendar {d}: {e}", file=sys.stderr)
            continue
        seen: dict[str, int] = {}
        picked: dict[tuple[str, int], dict] = {}
        for x in rows:
            if x.get("country") != "United States":
                continue
            key = (x.get("eventName") or "").strip().lower()
            n = seen.get(key, 0)
            seen[key] = n + 1
            picked[(key, n)] = x
        for key, n, label, tag, prio in CAL_WATCH:
            x = picked.get((key, n))
            if x is None:
                continue
            out.append({"date": d.isoformat(), "et": _cal_clean(x.get("gmt")) or "",
                        "name": label, "tag": tag, "prio": prio,
                        "cons": _cal_clean(x.get("consensus")), "prev": _cal_clean(x.get("previous"))})
    return out


def build_calendar() -> dict:
    """data/calendar.json: the next ~month of US macro releases plus Fed events, times in KST."""
    today = datetime.now(KST).date()
    end = today + timedelta(days=CAL_DAYS)
    ev = nasdaq_calendar(today, end)
    try:
        ev += fomc_events(today, end)
    except Exception as e:
        print(f"FOMC calendar: {e}", file=sys.stderr)
    now = datetime.now(KST)
    for e in ev:
        h, _, mi = (e.get("et") or "").partition(":")
        try:
            et = datetime.fromisoformat(e["date"]).replace(hour=int(h), minute=int(mi), tzinfo=ET)
        except ValueError:
            et = datetime.fromisoformat(e["date"]).replace(hour=9, tzinfo=ET)
        k = et.astimezone(KST)
        e["kst"] = k.strftime("%Y-%m-%d %H:%M")
        e["dday"] = (k.date() - today).days
        e["_t"] = k
    ev = sorted((e for e in ev if e["_t"] > now), key=lambda e: e["_t"])
    while len(ev) > CAL_MAX:  # trim the least important tier first, keeping chronological order
        worst = max(e["prio"] for e in ev)
        drop = next(i for i in range(len(ev) - 1, -1, -1) if ev[i]["prio"] == worst)
        ev.pop(drop)
    for e in ev:
        e.pop("_t")
    return {"updated_kst": now.strftime("%Y-%m-%d %H:%M"), "events": ev}


def _usd(v) -> str | None:
    """Nasdaq writes EPS as '$1.36' / '($0.04)' / '' ."""
    s = (v or "").replace("&nbsp;", "").strip()
    if not s or s in ("N/A", "$0.00"):
        return None
    neg = s.startswith("(")
    s = s.strip("()").lstrip("$")
    return ("−" if neg else "") + s


def build_earnings(tickers: set[str]) -> dict:
    """data/earnings.json: the next S&P 500 reports, soonest first, with the consensus EPS.
    Unlike the economic endpoint this one is not date-shifted (weekends and holidays come back empty)."""
    today = datetime.now(KST).date()
    out = []
    for i in range(ERN_DAYS + 1):
        if len(out) >= ERN_MAX:
            break
        d = today + timedelta(days=i)
        try:
            r = requests.get(NQ_ERN.format(d=d.isoformat()), headers=BROWSER | {"Referer": "https://www.nasdaq.com/"}, timeout=20)
            rows = ((r.json().get("data") or {}).get("rows")) or []
        except Exception as e:
            print(f"earnings {d}: {e}", file=sys.stderr)
            continue
        day = [x for x in rows if (x.get("symbol") or "").strip() in tickers]
        day.sort(key=lambda x: -_mcap(x.get("marketCap")))
        for x in day:
            out.append({
                "date": d.isoformat(), "dday": (d - today).days,
                "ticker": x["symbol"].strip(), "name": (x.get("name") or "").strip(),
                "when": {"time-pre-market": "장전", "time-after-hours": "장후"}.get(x.get("time"), "–"),
                "eps": _usd(x.get("epsForecast")), "eps_ly": _usd(x.get("lastYearEPS")),
                "fq": (x.get("fiscalQuarterEnding") or "").strip(),
            })
    return {"updated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "events": out[:ERN_MAX]}


def _mcap(s) -> float:
    try:
        return float((s or "").replace("$", "").replace(",", ""))
    except ValueError:
        return 0.0


def build_macro(prev: dict | None) -> tuple[dict, list[str], dict[str, pd.DataFrame], dict[str, pd.Series]]:
    """prev = previous latest.json payload; the non-Yahoo feeds (JGB, FRED, Fear & Greed) fall back to it when a source is down."""
    y_syms = [s for grp in MACRO.values() for _, s, _ in grp if not s.startswith(EXTERNAL)]
    ohlc = yahoo_ohlc(y_syms)
    closes = {sym: d["Close"] for sym, d in ohlc.items()}
    lines = {}  # non-Yahoo daily series -> line chart files
    for name, fn in (("JGB", jgb_series), ("FRED", lambda: fred_series([s.split(":")[1] for g in MACRO.values() for _, s, _ in g if s.startswith("FRED:")]))):
        try:
            lines.update(fn())
        except Exception as e:
            print(f"{name} fetch failed: {e}", file=sys.stderr)
    fng, fng_extra = fng_series()
    lines.update(fng)
    closes.update(lines)
    prev_items = {it["symbol"]: it for grp in (prev or {}).get("macro", {}).values() for it in grp}
    result, missing = {}, []
    for grp, items in MACRO.items():
        result[grp] = []
        for label, sym, kind in items:
            st = stats(closes[sym], kind) if sym in closes else None
            if st is None and sym.startswith(EXTERNAL) and prev_items.get(sym, {}).get("close") is not None:
                p = prev_items[sym]  # keep yesterday's reading rather than blanking the row
                print(f"{sym}: source down, reusing previous ({p['asof']})", file=sys.stderr)
                result[grp].append({**p, "name": label, "kind": kind})
                continue
            if st is None:
                missing.append(sym)
                st = {"close": None, "day": None, "m1": None, "asof": None}
            item = {"name": label, "symbol": sym, "kind": kind, "chart": sym in closes, **st}
            item.update(fng_extra.get(sym, {}))  # rating + hist for the gauge card
            result[grp].append(item)
    return result, missing, ohlc, lines


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


def build_stocks() -> tuple[list[dict], list[str], str, dict[str, pd.DataFrame]]:
    meta = json.loads((DATA / "sp500.json").read_text())["rows"]
    tickers = [r["ticker"] for r in meta]
    ohlc = yahoo_ohlc(tickers)
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
    return rows, missing, asof, ohlc


def main() -> int:
    latest = DATA / "latest.json"
    prev = json.loads(latest.read_text()) if latest.exists() else None
    macro, m_missing, m_ohlc, lines = build_macro(prev)
    stocks, s_missing, asof, s_ohlc = build_stocks()
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
            "fg": "CNN Fear & Greed 0-100; changes in points",
        },
    }
    # non-Yahoo feeds are best-effort: a missing reading is reported but never fails the run
    hard_missing = [m for m in m_missing if not m.startswith(EXTERNAL)]
    ok = asof is not None and len(hard_missing) == 0 and len(s_missing) <= 5
    prev_asof = prev.get("asof") if prev else None
    if not ok:
        print(f"FAIL asof={asof} macro_missing={m_missing} stocks_missing={len(s_missing)}", file=sys.stderr)
        return 2
    if prev_asof and asof < prev_asof:
        # Yahoo sometimes serves the latest daily bar as NaN for a while (seen ~20:30 ET); never regress.
        print(f"FAIL asof={asof} is older than previous {prev_asof}; keeping previous data", file=sys.stderr)
        return 2
    for sym, d in {**m_ohlc, **s_ohlc}.items():  # chart files only once the day's data is accepted
        write_ohlc(sym, d)
    for sym, s in lines.items():
        write_line(sym, s, dec=1 if sym.startswith("FNG:") else 3)
    if prev_asof == asof:
        print(f"no new trading day (asof {asof}); refreshing anyway for non-equity items")
    DATA.mkdir(exist_ok=True)
    HIST.mkdir(exist_ok=True)
    latest.write_text(json.dumps(payload, ensure_ascii=False))
    (HIST / f"{asof}.json").write_text(json.dumps(payload, ensure_ascii=False))
    for name, build in (("calendar", build_calendar),  # best-effort: keep the previous file if a source is down
                        ("earnings", lambda: build_earnings({s["ticker"] for s in stocks}))):
        try:
            d = build()
            if d["events"]:
                (DATA / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False))
            print(f"{name} events={len(d['events'])}")
        except Exception as e:
            print(f"{name} failed, keeping previous: {e}", file=sys.stderr)
    n_post = sum(1 for x in stocks if x["post"] is not None)
    print(f"OK asof={asof} stocks={len(stocks)} post={n_post} missing_stocks={s_missing} updated={payload['updated_kst']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
