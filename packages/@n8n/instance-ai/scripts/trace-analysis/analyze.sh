#!/usr/bin/env bash
# Reproducible end-to-end analysis: fetch -> classify -> notebook hint.
#
# Usage:
#   ./analyze.sh                           # last 7 days
#   ./analyze.sh --start 2026-04-21 --end 2026-04-28
#   ./analyze.sh --days 14
set -euo pipefail

cd "$(dirname "$0")"

START=""
END=""
DAYS=7
FORCE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --start) START="$2"; shift 2 ;;
    --end)   END="$2"; shift 2 ;;
    --days)  DAYS="$2"; shift 2 ;;
    --force) FORCE="--force"; shift ;;
    -h|--help)
      echo "Usage: $0 [--start YYYY-MM-DD --end YYYY-MM-DD] [--days N] [--force]"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -n "$START" ] && [ -n "$END" ]; then
  WINDOW="$(date -u -j -f '%Y-%m-%d' "$START" '+%Y%m%d')-$(date -u -j -f '%Y-%m-%d' "$END" '+%Y%m%d')"
  ./fetch.py --start "$START" --end "$END" $FORCE
else
  END_DATE="$(date -u '+%Y-%m-%d')"
  START_DATE="$(date -u -v-"${DAYS}"d '+%Y-%m-%d')"
  WINDOW="$(date -u -j -f '%Y-%m-%d' "$START_DATE" '+%Y%m%d')-$(date -u -j -f '%Y-%m-%d' "$END_DATE" '+%Y%m%d')"
  ./fetch.py --days "$DAYS" $FORCE
fi

./classify.py --window "$WINDOW"

echo
echo "Open the notebook:  jupyter lab analysis.ipynb"
echo "Or in VS Code:      code analysis.ipynb"
echo "Set WINDOW=$WINDOW in the first cell."
