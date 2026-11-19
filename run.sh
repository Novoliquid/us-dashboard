#!/bin/zsh
# Daily update: fetch -> commit -> push (GitHub Pages serves the repo root).
# Exit 0 = pushed, 2 = fetch failed (previous data kept), 3 = push failed.
set -u
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG=data/run.log
ts() { date "+%Y-%m-%d %H:%M:%S"; }

# weekly constituent refresh (Mondays) or if missing
if [[ ! -f data/sp500.json || "$(date +%u)" == "1" ]]; then
  uv run python constituents.py >>"$LOG" 2>&1 || echo "$(ts) constituents refresh failed (kept previous)" >>"$LOG"
fi

# keep data/fund in step with the index: fundamentals otherwise only refresh weekly, so a new
# entrant would sit without a file for days. Add the newcomers, drop the leavers.
NEW=$(python3 - <<'EOF'
import json, os
tk = {s["ticker"] for s in json.load(open("data/sp500.json"))["rows"]}
have = {f[:-5] for f in os.listdir("data/fund") if f.endswith(".json")}
for t in sorted(have - tk):
    os.remove(f"data/fund/{t}.json")
print(" ".join(sorted(tk - have)))
EOF
)
if [[ -n "$NEW" ]]; then
  echo "$(ts) fund: new constituents $NEW" >>"$LOG"
  uv run python fund.py ${=NEW} >>"$LOG" 2>&1 || echo "$(ts) fund backfill failed" >>"$LOG"
fi

# Snapshot the payload we are about to replace. Yahoo sometimes serves a mid-session price as the
# close right after the US bell (seen 2026-11-19: 499/502 tickers revised between the 07:00 and
# 11:30 runs, same asof). The stale guard in fetch.py only blocks older asof values, so a same-day
# revision is invisible to it — count it here instead.
cp data/latest.json /tmp/prev_latest.json 2>/dev/null || rm -f /tmp/prev_latest.json

if ! uv run python fetch.py >>"$LOG" 2>&1; then
  echo "$(ts) FETCH FAILED" >>"$LOG"
  tail -5 "$LOG"
  exit 2
fi

git add -A data index.html stock.html >/dev/null 2>&1
if git diff --cached --quiet; then
  echo "$(ts) nothing changed" >>"$LOG"
else
  ASOF=$(python3 -c "import json;print(json.load(open('data/latest.json'))['asof'])")
  git commit -q -m "data: $ASOF ($(date +%Y-%m-%d\ %H:%M) KST)" || true
  if ! git push -q origin HEAD >>"$LOG" 2>&1; then
    echo "$(ts) PUSH FAILED" >>"$LOG"
    exit 3
  fi
fi

# one-line summary for notifications
python3 - <<'EOF'
import json
d = json.load(open("data/latest.json"))
idx = {i["name"]: i for i in d["macro"]["Indices"]}
def f(x): return f"{x:+.2f}%"
by = {}
for s in d["stocks"]:
    if s.get("ca"):  # unadjusted spin-off/split: not a real move
        continue
    by.setdefault(s["sector"], []).append(s["day"])
avg = {k: sum(v)/len(v) for k, v in by.items() if v}
best = max(avg, key=avg.get); worst = min(avg, key=avg.get)

# same-asof revisions: how much of the previous run turned out to be provisional
rev = ""
try:
    p = json.load(open("/tmp/prev_latest.json"))
    if p["asof"] == d["asof"]:
        old = {s["ticker"]: s["day"] for s in p["stocks"]}
        n = sum(1 for s in d["stocks"] if s["ticker"] in old and s["day"] != old[s["ticker"]])
        if n:
            rev = f" | REVISED {n}/{len(d['stocks'])} vs {p['updated_kst']}"
except Exception:
    pass

print(f"US {d['asof']} | Dow {f(idx['Dow Jones']['day'])} S&P {f(idx['S&P 500']['day'])} Nasdaq {f(idx['Nasdaq']['day'])} | "
      f"best {best} {f(avg[best])} / worst {worst} {f(avg[worst])} | missing {len(d['missing']['stocks'])}{rev}")
EOF
