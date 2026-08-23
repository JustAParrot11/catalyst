#!/usr/bin/env bash
# Run the whole test suite at many future dates.
#
# WHY THIS EXISTS. install/upgrade.sh runs the suite before it will
# install anything, so a test that fails on a future date blocks EVERY
# upgrade from that day onward - and more often than not it is the CODE
# that is wrong at that date, not the test. This has bitten the owner
# twice: once on a weekend, once from 1 September.
#
# The bot is meant to run unattended forever. A handful of hand-picked
# dates cannot establish that; it only checks the dates somebody already
# suspected. So this sweeps DENSELY and includes the boundaries that
# have historically broken things:
#
#   month firsts    month-to-date arithmetic resets, and "yesterday" is
#                   in the previous month
#   month lasts     windows that run to the end of a month
#   year rollovers  and the day after
#   29 February     a leap day exists in 2028 and not in 2027
#   weekends        the market is shut and the calendar helpers differ
#   far future      anything anchored to a literal date has expired
#
# It restores the clock from the elapsed monotonic time rather than
# trusting the wall clock it just moved, so an interrupted run does not
# leave the machine in the future.
#
# Usage: sudo scripts/clock_sweep.sh [output-file]
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-/tmp/clock_sweep.txt}"

DATES=(
  # month firsts - a year of them
  "2026-09-01" "2026-10-01" "2026-11-01" "2026-12-01"
  "2027-01-01" "2027-02-01" "2027-03-01" "2027-04-01"
  "2027-05-01" "2027-06-01" "2027-07-01" "2027-08-01"
  # month lasts, including a 30-day month and a 28-day February
  "2026-08-31" "2026-09-30" "2026-11-30" "2027-02-28"
  # known boundaries in this codebase
  "2026-11-08"   # the pricing table declares itself stale
  "2027-12-31"   # year end
  # leap day, and the day after
  "2028-02-29" "2028-03-01"
  # weekends
  "2026-09-05" "2026-09-06"
  # far enough out that anything anchored to a literal date has expired
  "2029-06-15"
)

: > "$OUT"
START=$(date +%s); UP0=$(cut -d' ' -f1 /proc/uptime)
FAILED=0
for d in "${DATES[@]}"; do
  date -s "$d 12:00:00" >/dev/null 2>&1
  dow=$(date +%a)
  printf '===== %s (%s) =====\n' "$d" "$dow" >> "$OUT"
  find "$REPO" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  ( cd "$REPO" && python -m pytest -p no:randomly 2>&1 \
      | grep -E "^FAILED|passed|failed" ) >> "$OUT"
  grep -q "^FAILED" <(tail -20 "$OUT") && FAILED=1
  sync
done

# Restore from ELAPSED MONOTONIC time, never from the wall clock this
# script has been moving around.
UP1=$(cut -d' ' -f1 /proc/uptime)
date -s "@$(python3 -c "print(int($START + $UP1 - $UP0))")" >/dev/null 2>&1
printf 'CLOCK RESTORED -> %s\n' "$(date -u)" >> "$OUT"
printf 'SWEEP COMPLETE (%d dates)\n' "${#DATES[@]}" >> "$OUT"
sync
exit $FAILED
