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

if ! uv run python fetch.py >>"$LOG" 2>&1; then
  echo "$(ts) FETCH FAILED" >>"$LOG"
  tail -5 "$LOG"
  exit 2
fi

git add -A data index.html >/dev/null 2>&1
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
    by.setdefault(s["sector"], []).append(s["day"])
avg = {k: sum(v)/len(v) for k, v in by.items() if v}
best = max(avg, key=avg.get); worst = min(avg, key=avg.get)
print(f"US {d['asof']} | Dow {f(idx['Dow Jones']['day'])} S&P {f(idx['S&P 500']['day'])} Nasdaq {f(idx['Nasdaq']['day'])} | "
      f"best {best} {f(avg[best])} / worst {worst} {f(avg[worst])} | missing {len(d['missing']['stocks'])}")
EOF
