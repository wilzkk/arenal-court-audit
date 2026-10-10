#!/usr/bin/env bash
# Check court availability every INTERVAL_MIN minutes for LOOP_MINUTES minutes,
# saving data and report to the repo after every check.
set -u

INTERVAL_MIN="${INTERVAL_MIN:-15}"
LOOP_MINUTES="${LOOP_MINUTES:-340}"
end=$(( $(date +%s) + LOOP_MINUTES * 60 ))

git config user.name "court-audit-bot"
git config user.email "court-audit-bot@users.noreply.github.com"

python3 persist.py load

while :; do
  if python3 tracker.py --stopped; then
    echo "audit period ended, stopping"
    break
  fi
  started=$(date +%s)

  python3 tracker.py || echo "check failed (logged in runs table)"
  python3 report.py > /dev/null || echo "report failed"
  python3 persist.py dump

  git add data/audit.sql report.html
  if ! git diff --cached --quiet; then
    git commit -q -m "check $(date -u +%Y-%m-%dT%H:%MZ)"
    git push -q || { git pull -q --rebase && git push -q; } || echo "push failed, will retry next check"
  fi

  next=$(( started + INTERVAL_MIN * 60 ))
  if [ "$next" -ge "$end" ]; then
    echo "loop window over"
    break
  fi
  wait=$(( next - $(date +%s) ))
  [ "$wait" -gt 0 ] && sleep "$wait"
done
