#!/bin/zsh
# Weekly fundamentals refresh: fund.py -> data/fund/*.json -> commit -> push.
# Exit 0 = pushed (or nothing changed), 2 = fetch failed, 3 = push failed.
set -u
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG=data/run.log
ts() { date "+%Y-%m-%d %H:%M:%S"; }

if ! uv run python fund.py >>"$LOG" 2>&1; then
  echo "$(ts) FUND FAILED" >>"$LOG"
  tail -5 "$LOG"
  exit 2
fi

git add -A data/fund >/dev/null 2>&1
if git diff --cached --quiet; then
  echo "$(ts) fund: nothing changed" >>"$LOG"
else
  git commit -q -m "fund: weekly refresh ($(date +%Y-%m-%d) KST)" || true
  if ! git push -q origin HEAD >>"$LOG" 2>&1; then
    echo "$(ts) FUND PUSH FAILED" >>"$LOG"
    exit 3
  fi
fi
grep "^DONE" "$LOG" | tail -1
