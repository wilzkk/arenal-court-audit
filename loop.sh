#!/usr/bin/env bash
# Check court availability every INTERVAL_MIN minutes for LOOP_MINUTES minutes.
# The database stays on the runner between checks; it is saved to the repo
# every SAVE_EVERY checks (default: hourly) and when the loop ends, which
# keeps the repo small even with all clubs tracked.
set -u

INTERVAL_MIN="${INTERVAL_MIN:-15}"
LOOP_MINUTES="${LOOP_MINUTES:-340}"
SAVE_EVERY="${SAVE_EVERY:-4}"
end=$(( $(date +%s) + LOOP_MINUTES * 60 ))

git config user.name "court-audit-bot"
git config user.email "court-audit-bot@users.noreply.github.com"

save() {
  python3 report.py > /dev/null || echo "report failed"
  python3 persist.py dump
  git add data/audit.sql report.html directie.html
  if ! git diff --cached --quiet; then
    git commit -q -m "data $(date -u +%Y-%m-%dT%H:%MZ)"
    git push -q && return
    if git pull -q --rebase; then
      git push -q && return
    else
      git rebase --abort 2>/dev/null   # never leave the checkout stuck mid-rebase
    fi
    echo "push failed, will retry at next save"
  fi
}

# A queued run is checked out at the commit from when it was queued, which can be
# hours behind the data the previous run saved. Start from the latest data.
git fetch -q origin main && git reset -q --hard FETCH_HEAD \
  || echo "could not update to the latest main, using the checkout"
python3 persist.py load

n=0
while :; do
  if python3 tracker.py --stopped; then
    echo "audit period ended, stopping"
    break
  fi
  started=$(date +%s)
  n=$(( n + 1 ))

  python3 tracker.py || echo "check failed (logged in runs table)"
  if [ "$n" -eq 1 ] || [ $(( n % SAVE_EVERY )) -eq 0 ]; then save; fi

  next=$(( started + INTERVAL_MIN * 60 ))
  if [ "$next" -ge "$end" ]; then
    echo "loop window over"
    break
  fi
  wait=$(( next - $(date +%s) ))
  [ "$wait" -gt 0 ] && sleep "$wait"
done

save
