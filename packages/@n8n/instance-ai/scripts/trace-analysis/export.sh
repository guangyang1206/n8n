#!/usr/bin/env bash
# Export the analysis notebook to standalone HTML (and optionally PDF).
#
# Usage:
#   ./export.sh                    # last 7 days, HTML only
#   ./export.sh --window 20260421-20260428
#   ./export.sh --pdf              # also produce a flat PDF via Chromium
set -euo pipefail
cd "$(dirname "$0")"

WINDOW=""
WANT_PDF=0
while [ $# -gt 0 ]; do
  case "$1" in
    --window) WINDOW="$2"; shift 2 ;;
    --pdf)    WANT_PDF=1; shift ;;
    -h|--help)
      echo "Usage: $0 [--window YYYYMMDD-YYYYMMDD] [--pdf]"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$WINDOW" ]; then
  END_DATE="$(date -u '+%Y-%m-%d')"
  START_DATE="$(date -u -v-7d '+%Y-%m-%d')"
  WINDOW="$(date -u -j -f '%Y-%m-%d' "$START_DATE" '+%Y%m%d')-$(date -u -j -f '%Y-%m-%d' "$END_DATE" '+%Y%m%d')"
fi

mkdir -p dist
OUT="dist/analysis-${WINDOW}.html"

# Set the WINDOW in cell 1 before exporting, so the HTML reflects this window.
uv run --quiet --with nbformat python - <<PY
import nbformat
nb = nbformat.read('analysis.ipynb', as_version=4)
src = nb.cells[1].source
# Replace the WINDOW = '...' line
import re
nb.cells[1].source = re.sub(r"WINDOW\s*=\s*'[^']*'", "WINDOW = '${WINDOW}'", src, count=1)
nbformat.write(nb, 'analysis.ipynb')
PY

# Re-execute so outputs reflect the chosen window.
uv run --quiet --with 'nbformat' --with 'jupyter' --with 'nbclient' \
  --with 'pandas' --with 'plotly' --with 'ipykernel' --with 'numpy' python - <<'PY'
import nbformat
from nbclient import NotebookClient
nb = nbformat.read('analysis.ipynb', as_version=4)
NotebookClient(nb, timeout=300, kernel_name='python3').execute()
nbformat.write(nb, 'analysis.ipynb')
PY

uv run --quiet --with 'nbconvert' --with 'jinja2' \
  jupyter nbconvert --to html --embed-images --no-input analysis.ipynb --output "$OUT"
echo "✓ HTML: $OUT"

if [ "$WANT_PDF" = "1" ]; then
  PDF="dist/analysis-${WINDOW}.pdf"
  # webpdf needs Playwright + Chromium.
  uv run --quiet --with 'nbconvert' --with 'playwright' --with 'jinja2' bash -c "\
    playwright install chromium >/dev/null 2>&1 || true; \
    jupyter nbconvert --to webpdf --no-input --allow-chromium-download analysis.ipynb --output '$PDF'"
  echo "✓ PDF:  $PDF"
fi
