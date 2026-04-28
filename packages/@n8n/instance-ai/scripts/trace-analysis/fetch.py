#!/usr/bin/env -S uv run --quiet --with langsmith>=0.4,<0.5 --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["langsmith>=0.4,<0.5"]
# ///
"""Fetch raw trace data for a window from LangSmith.

Three streaming queries (cursor-paginated, 100/page max):

  Q1 builders     — name = subagent:workflow-builder
  Q2 llm_children — descendants of those builders, run_type = llm
  Q3 tool_children — descendants of those builders, run_type = tool

Output: data/raw/<window>/{builders,llm_children,tool_children}.jsonl

Idempotent: skips files that already exist unless --force.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langsmith import Client

PROJECT = os.environ.get("INSTANCE_AI_PROJECT", "instance-ai")
BUILDER_NAME = "subagent:workflow-builder"
HERE = Path(__file__).parent


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="ISO date (UTC), e.g. 2026-04-21")
    p.add_argument("--end", help="ISO date (UTC), e.g. 2026-04-28")
    p.add_argument("--days", type=int, default=7, help="When --start/--end omitted, last N days")
    p.add_argument("--force", action="store_true", help="Refetch even if cache exists")
    p.add_argument("--out", default=str(HERE / "data" / "raw"))
    return p.parse_args()


def resolve_window(args):
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    start = (
        datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        if args.start
        else end - timedelta(days=args.days)
    )
    label = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
    return start, end, label


def serialize_run(r):
    """Trim a Run to the fields we use, JSON-serializable."""
    md = r.metadata or {}
    out = r.outputs or {}
    return {
        "id": str(r.id),
        "name": r.name,
        "run_type": r.run_type,
        "trace_id": str(r.trace_id) if r.trace_id else None,
        "parent_run_id": str(r.parent_run_id) if r.parent_run_id else None,
        "start_time": r.start_time.isoformat() if r.start_time else None,
        "end_time": r.end_time.isoformat() if r.end_time else None,
        "status": r.status,
        "error": r.error,
        "total_tokens": r.total_tokens,
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        # Metadata: keep just the keys we use downstream.
        "thread_id": md.get("thread_id"),
        "agent_role": md.get("agent_role"),
        "final_status": md.get("final_status"),
        "message_group_id": md.get("message_group_id"),
        # Outputs: result/status only — full IO is big and not used.
        "result": out.get("result") if isinstance(out, dict) else None,
        "result_status": out.get("status") if isinstance(out, dict) else None,
    }


def stream_to_jsonl(client, path, label, **list_runs_kwargs):
    """Stream list_runs results to a JSONL file. Returns row count."""
    print(f"  → {label} ...", flush=True)
    t0 = time.time()
    n = 0
    with path.open("w") as f:
        for r in client.list_runs(**list_runs_kwargs):
            f.write(json.dumps(serialize_run(r)) + "\n")
            n += 1
            if n % 1000 == 0:
                print(f"     {n} rows ({time.time() - t0:.0f}s)...", flush=True)
    print(f"  ✓ {label}: {n} rows in {time.time() - t0:.1f}s", flush=True)
    return n


def main():
    args = parse_args()
    start, end, label = resolve_window(args)
    out_dir = Path(args.out) / label
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Window: {start.isoformat()} → {end.isoformat()} ({label})")
    print(f"Output: {out_dir}")

    client = Client()

    targets = {
        "builders.jsonl": dict(
            project_name=PROJECT,
            filter=f'eq(name, "{BUILDER_NAME}")',
            start_time=start,
            end_time=end,
        ),
        "llm_children.jsonl": dict(
            project_name=PROJECT,
            filter='eq(run_type, "llm")',
            trace_filter=f'eq(name, "{BUILDER_NAME}")',
            start_time=start,
            end_time=end,
        ),
        "tool_children.jsonl": dict(
            project_name=PROJECT,
            filter='eq(run_type, "tool")',
            trace_filter=f'eq(name, "{BUILDER_NAME}")',
            start_time=start,
            end_time=end,
        ),
    }

    counts = {}
    for fname, kwargs in targets.items():
        path = out_dir / fname
        if path.exists() and not args.force:
            n = sum(1 for _ in path.open())
            print(f"  • {fname}: cached ({n} rows) — use --force to refetch")
            counts[fname] = n
            continue
        counts[fname] = stream_to_jsonl(client, path, fname, **kwargs)

    manifest = {
        "window": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "project": PROJECT,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest: {out_dir / 'manifest.json'}")
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    sys.exit(main())
