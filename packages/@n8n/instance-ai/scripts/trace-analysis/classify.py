#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Classify raw fetched data into per-builder + per-thread violations.

Reads data/raw/<window>/*.jsonl, writes data/classified/<window>.json.

Purely local computation — no LangSmith network calls. Safe to re-run.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from heuristics import rules, thresholds  # noqa: E402


# Tool names of interest (full LangSmith run names include the "tool:" prefix).
TOOL_EXEC = "tool:mastra_workspace_execute_command"
TOOL_EDIT = "tool:mastra_workspace_edit_file"
TOOL_WRITE = "tool:mastra_workspace_write_file"
TOOL_SUBMIT = "tool:submit-workflow"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--window", required=True, help="e.g. 20260421-20260428")
    p.add_argument("--raw-dir", default=str(HERE / "data" / "raw"))
    p.add_argument("--out-dir", default=str(HERE / "data" / "classified"))
    return p.parse_args()


def read_jsonl(path):
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def latency_seconds(row):
    if not row.get("start_time") or not row.get("end_time"):
        return None
    s = datetime.fromisoformat(row["start_time"])
    e = datetime.fromisoformat(row["end_time"])
    return (e - s).total_seconds()


def build_per_builder(builders, llm_children, tool_children):
    """Aggregate child counts onto each builder by trace_id.

    Builders are root runs — their trace_id == their own run id.
    Children share the same trace_id.
    """
    llm_by_trace = Counter(c["trace_id"] for c in llm_children if c.get("trace_id"))
    tool_by_trace_name = Counter(
        (c["trace_id"], c["name"]) for c in tool_children if c.get("trace_id") and c.get("name")
    )

    out = []
    for b in builders:
        tid = b["trace_id"]
        out.append({
            "run_id": b["id"],
            "trace_id": tid,
            "thread_id": b.get("thread_id"),
            "start_time": b.get("start_time"),
            "end_time": b.get("end_time"),
            "status": b.get("status"),
            "final_status": b.get("final_status"),
            "result_status": b.get("result_status"),
            "tokens": b.get("total_tokens") or 0,
            "latency_s": latency_seconds(b),
            "llm_calls": llm_by_trace.get(tid, 0),
            "exec_calls": tool_by_trace_name.get((tid, TOOL_EXEC), 0),
            "edit_calls": tool_by_trace_name.get((tid, TOOL_EDIT), 0),
            "write_calls": tool_by_trace_name.get((tid, TOOL_WRITE), 0),
            "submit_calls": tool_by_trace_name.get((tid, TOOL_SUBMIT), 0),
        })
    return out


def build_per_thread(per_builder):
    by_thread = defaultdict(list)
    for b in per_builder:
        if b.get("thread_id"):
            by_thread[b["thread_id"]].append(b)
    return by_thread


def main():
    args = parse_args()
    raw = Path(args.raw_dir) / args.window
    if not raw.exists():
        print(f"ERROR: no raw data at {raw}. Run fetch.py first.", file=sys.stderr)
        return 1

    builders = read_jsonl(raw / "builders.jsonl")
    llm_children = read_jsonl(raw / "llm_children.jsonl")
    tool_children = read_jsonl(raw / "tool_children.jsonl")
    print(f"Loaded: {len(builders)} builders, {len(llm_children)} llm, {len(tool_children)} tool")

    per_builder = build_per_builder(builders, llm_children, tool_children)
    t = thresholds.compute(per_builder)
    print(f"Thresholds (p95 ∨ floor): {t.to_dict()}")

    # Apply per-builder rules
    for b in per_builder:
        b["violations"] = rules.evaluate(b, t)

    # Per-thread
    by_thread = build_per_thread(per_builder)
    threads = []
    violation_counter = Counter()
    for tid, builder_list in by_thread.items():
        builder_list.sort(key=lambda x: x.get("start_time") or "")
        thread_violations = []
        retry = rules.thread_retry(builder_list)
        if retry:
            thread_violations.append(retry)
        codes = {v["code"] for b in builder_list for v in b["violations"]}
        codes.update(v["code"] for v in thread_violations)
        for c in codes:
            violation_counter[c] += 1
        threads.append({
            "thread_id": tid,
            "builder_count": len(builder_list),
            "first_start": builder_list[0].get("start_time"),
            "last_end": builder_list[-1].get("end_time"),
            "fail_count": sum(
                1 for b in builder_list
                if b["status"] == "error" or b["final_status"] in ("failed", "cancelled")
            ),
            "total_tokens": sum(b["tokens"] for b in builder_list),
            "violation_codes": sorted(codes),
            "thread_violations": thread_violations,
        })
    threads.sort(key=lambda x: x["first_start"] or "")

    # Per-builder violations summary (counts, not unique threads)
    builder_violation_counter = Counter()
    for b in per_builder:
        for v in b["violations"]:
            builder_violation_counter[v["code"]] += 1

    summary = {
        "window": args.window,
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": t.to_dict(),
        "totals": {
            "builders": len(per_builder),
            "threads": len(by_thread),
            "builders_failed": sum(
                1 for b in per_builder
                if b["status"] == "error" or b["final_status"] == "failed"
            ),
            "builders_cancelled": sum(
                1 for b in per_builder if b["final_status"] == "cancelled"
            ),
        },
        "violation_counts_per_builder": dict(builder_violation_counter),
        "violation_counts_per_thread": dict(violation_counter),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.window}.json"
    out_path.write_text(json.dumps({
        "summary": summary,
        "builders": per_builder,
        "threads": threads,
    }, indent=2))
    print(f"\nWrote {out_path}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
