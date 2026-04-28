# Instance AI — Trace Failure Analysis

Reproducible analysis of `instance-ai` LangSmith traces, focused on failure
modes of the `workflow-builder` sub-agent.

## Quick start

```bash
# Default window: last 7 days (UTC)
./analyze.sh

# Explicit window
./analyze.sh --start 2026-04-21 --end 2026-04-28
```

This runs `fetch.py`, then `classify.py`, then prints the path to
`analysis.ipynb` to open in Jupyter / VS Code.

Requires `LANGSMITH_API_KEY` in env. Uses `uv` with inline deps — no
virtualenv setup needed.

## Layout

```
trace-analysis/
├── README.md
├── analyze.sh             # one-shot: fetch -> classify -> notebook
├── fetch.py               # LangSmith -> data/raw/<window>/
├── classify.py            # raw -> data/classified/<window>.json
├── analysis.ipynb         # dashboard (reads classified.json)
├── heuristics/
│   ├── __init__.py
│   ├── thresholds.py      # dynamic p95 + hard floor
│   └── rules.py           # one pure function per heuristic
└── data/
    ├── raw/<window>/      # cached LangSmith dump
    └── classified/        # per-window classifications
```

## Heuristics

Each builder invocation is labelled with zero or more violations.
Thresholds are **dynamic = p95 of the window**, with a **hard floor** so a
quiet week can't lower the bar.

| # | Heuristic | Floor | Captures |
|---|---|---|---|
| 1 | Step explosion | 15 LLM calls | LLM loop |
| 2 | TS-error loop | 5 `execute_command` | Builder fighting `tsc` |
| 3 | Edit thrash | 7 `edit_file` | Repeated patching |
| 4 | Write thrash | 4 `write_file` | Rewriting from scratch |
| 5 | Submit loop | 3 `submit-workflow` | Publish-fail-publish |
| 6 | Latency outlier | 600s | Slow regardless of cause |
| 7 | Token outlier | 500k | Cost regardless of cause |
| 8 | Hard failure | — | `status=error` or `final_status=failed` |
| 9 | Cancelled | — | `final_status=cancelled` |
| 10 | Builder retry in thread | ≥2 | Multiple builders, ≥1 failed |

## Reproducibility

The same `--start`/`--end` window produces the same `classified.json`,
modulo what was in LangSmith at fetch time. Raw fetches are cached so
re-classification is instant.
