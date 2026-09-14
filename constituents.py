"""S&P 500 constituents + GICS sector from Wikipedia -> data/sp500.json

Run weekly. Yahoo uses '-' where Wikipedia uses '.' (BRK.B -> BRK-B).
"""
import json
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor

URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
OUT = Path(__file__).parent / "data" / "sp500.json"
UA = {"User-Agent": "Mozilla/5.0 (us-dashboard; personal use)"}


def main() -> int:
    html = requests.get(URL, headers=UA, timeout=30).text
    tables = pd.read_html(StringIO(html), attrs={"id": "constituents"})
    if not tables:
        print("constituents table not found", file=sys.stderr)
        return 1
    df = tables[0]
    rows = []
    for _, r in df.iterrows():
        sym = str(r["Symbol"]).strip()
        rows.append(
            {
                "ticker": sym.replace(".", "-"),
                "name": str(r["Security"]).strip(),
                "sector": str(r["GICS Sector"]).strip(),
                "industry": str(r["GICS Sub-Industry"]).strip(),
            }
        )
    if len(rows) < 480:
        print(f"only {len(rows)} rows, refusing to overwrite", file=sys.stderr)
        return 1

    # shares outstanding for market-cap sorting; daily job multiplies by close
    prev = {}
    if OUT.exists():
        prev = {r["ticker"]: r.get("shares") for r in json.loads(OUT.read_text())["rows"]}

    def shares(t: str):
        try:
            return t, int(yf.Ticker(t).fast_info["shares"])
        except Exception:
            return t, prev.get(t)

    with ThreadPoolExecutor(8) as ex:
        got = dict(ex.map(shares, [r["ticker"] for r in rows]))
    missing = [t for t, v in got.items() if not v]
    for r in rows:
        r["shares"] = got.get(r["ticker"])
    if missing:
        print(f"shares missing for {len(missing)}: {missing[:10]}", file=sys.stderr)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps(
            {"updated": datetime.now(timezone.utc).isoformat(), "count": len(rows), "rows": rows},
            ensure_ascii=False,
            indent=1,
        )
    )
    sectors = sorted({r["sector"] for r in rows})
    print(f"{len(rows)} constituents, {len(sectors)} sectors -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
