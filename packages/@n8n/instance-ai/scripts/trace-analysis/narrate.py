#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["langsmith>=0.4,<0.5", "anthropic>=0.40", "tenacity>=8.0"]
# ///
"""Sample threads, fetch full I/O, ask Opus for failure narratives, cluster.

Pipeline:
  1. Read data/classified/<window>.json
  2. Stratified-sample threads:
       cluster by violation-code tuple, take proportional (cap=10) per cluster,
       plus top-K severity outliers regardless of cluster.
  3. For each sampled thread, refetch the builder runs + tool children WITH
     inputs/outputs from LangSmith.
  4. Per-thread Opus call (max 5 concurrent) → structured narrative JSON.
  5. Single Opus call to cluster pattern_tags into themes.
  6. Write data/narrated/<window>.json.

Re-run is idempotent at the per-thread level: existing narratives are kept
unless --force.
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from anthropic import AsyncAnthropic
from langsmith import Client as LSClient
from tenacity import retry, stop_after_attempt, wait_exponential

HERE = Path(__file__).parent
PROJECT = os.environ.get("INSTANCE_AI_PROJECT", "instance-ai")
MODEL = os.environ.get("NARRATE_MODEL", "claude-opus-4-7")
SAMPLE_SIZE = 100
MAX_CONCURRENT = 5
PER_CLUSTER_CAP = 10
OUTLIER_TAIL = 10
TOOL_INPUT_TRUNC = 600
RESULT_TRUNC = 1500
# Caps to keep prompts within reason for long retry threads.
MAX_BUILDERS_PER_THREAD = 8
MAX_TOOLS_PER_BUILDER = 25
# Concurrency for the LangSmith I/O refetch step (separate from LLM calls).
IO_FETCH_CONCURRENCY = 3


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--window", required=True, help="e.g. 20260421-20260428")
    p.add_argument("--classified-dir", default=str(HERE / "data" / "classified"))
    p.add_argument("--out-dir", default=str(HERE / "data" / "narrated"))
    p.add_argument("--sample-size", type=int, default=SAMPLE_SIZE)
    p.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT)
    p.add_argument("--force", action="store_true", help="Re-narrate threads even if cached")
    p.add_argument("--dry-run", action="store_true", help="Pick sample + refetch I/O only, skip LLM")
    return p.parse_args()


# ───────────────────────────────────────────────────── sampling ──

def severity_score(thread, builders_by_thread, thresholds):
    """Composite severity = sum of metric/threshold ratios across thread's builders."""
    total = 0.0
    for b in builders_by_thread.get(thread["thread_id"], []):
        for k, t in (
            ("llm_calls", thresholds["llm_calls"]),
            ("exec_calls", thresholds["exec_calls"]),
            ("edit_calls", thresholds["edit_calls"]),
            ("write_calls", thresholds["write_calls"]),
            ("submit_calls", thresholds["submit_calls"]),
            ("tokens", thresholds["tokens"]),
            ("latency_s", thresholds["latency_s"]),
        ):
            v = b.get(k) or 0
            if t and v:
                total += v / t
    return total


def select_sample(classified, target_size):
    threads = classified["threads"]
    builders = classified["builders"]
    thresholds = classified["summary"]["thresholds"]

    # Only consider threads with at least one violation
    flagged = [t for t in threads if t.get("violation_codes")]
    if not flagged:
        return []

    builders_by_thread = defaultdict(list)
    for b in builders:
        if b.get("thread_id"):
            builders_by_thread[b["thread_id"]].append(b)

    # Cluster by violation-code tuple
    clusters = defaultdict(list)
    for t in flagged:
        key = tuple(sorted(t["violation_codes"]))
        clusters[key].append(t)

    # Proportional allocation, capped per cluster
    total_flagged = len(flagged)
    cluster_budget = max(1, target_size - OUTLIER_TAIL)
    selected = {}
    for key, ts in clusters.items():
        share = max(1, round(cluster_budget * len(ts) / total_flagged))
        share = min(share, PER_CLUSTER_CAP, len(ts))
        # Within cluster, prefer most severe
        ts_sorted = sorted(
            ts,
            key=lambda t: severity_score(t, builders_by_thread, thresholds),
            reverse=True,
        )
        for t in ts_sorted[:share]:
            selected[t["thread_id"]] = t

    # Outlier tail: top by severity, regardless of cluster
    flagged_sorted = sorted(
        flagged,
        key=lambda t: severity_score(t, builders_by_thread, thresholds),
        reverse=True,
    )
    for t in flagged_sorted[:OUTLIER_TAIL]:
        selected[t["thread_id"]] = t

    return list(selected.values())[:target_size]


# ──────────────────────────────────────────── full I/O refetch ──

def truncate(s, n):
    if s is None:
        return None
    if not isinstance(s, str):
        s = json.dumps(s, default=str)
    return s if len(s) <= n else s[:n] + f"…(+{len(s)-n} chars)"


def _select_builders_to_dump(thread_builders):
    """If a thread has too many builders, prefer failed ones plus the most recent."""
    if len(thread_builders) <= MAX_BUILDERS_PER_THREAD:
        return sorted(thread_builders, key=lambda x: x.get("start_time") or "")
    failed = [b for b in thread_builders if b["status"] == "error" or b["final_status"] in ("failed", "cancelled")]
    failed_sorted = sorted(failed, key=lambda x: x.get("start_time") or "")
    others_sorted = sorted(
        [b for b in thread_builders if b not in failed],
        key=lambda x: x.get("start_time") or "",
    )
    keep = failed_sorted[:MAX_BUILDERS_PER_THREAD]
    if len(keep) < MAX_BUILDERS_PER_THREAD:
        keep += others_sorted[-(MAX_BUILDERS_PER_THREAD - len(keep)):]
    return sorted(keep, key=lambda x: x.get("start_time") or "")


def _serialize_builder_io(builder_run, tool_children):
    bin_ = builder_run.inputs or {}
    bout = builder_run.outputs or {}
    tool_children = sorted(
        tool_children, key=lambda r: r.start_time or datetime.min.replace(tzinfo=timezone.utc)
    )
    if len(tool_children) > MAX_TOOLS_PER_BUILDER:
        # Keep the first 5 (setup) and the last (MAX-5) (where failure usually shows).
        head = tool_children[:5]
        tail = tool_children[-(MAX_TOOLS_PER_BUILDER - 5):]
        tool_children = head + tail
    return {
        "trace_id": str(builder_run.id),
        "task": truncate(bin_.get("task") if isinstance(bin_, dict) else bin_, 1500),
        "result": truncate(bout.get("result") if isinstance(bout, dict) else bout, RESULT_TRUNC),
        "result_status": (bout.get("status") if isinstance(bout, dict) else None),
        "status": builder_run.status,
        "final_status": (builder_run.metadata or {}).get("final_status"),
        "error": truncate(builder_run.error, 800),
        "tools": [
            {
                "name": (r.name or "").removeprefix("tool:"),
                "input": truncate(r.inputs, TOOL_INPUT_TRUNC),
                "output": truncate(r.outputs, TOOL_INPUT_TRUNC),
                "error": truncate(r.error, 400) if r.error else None,
            }
            for r in tool_children
        ],
    }


@retry(stop=stop_after_attempt(8), wait=wait_exponential(multiplier=3, min=5, max=180))
def fetch_thread_io(client, thread, builders_by_thread):
    """Pull builders + their tool children with full I/O for one thread."""
    thread_builders = builders_by_thread.get(thread["thread_id"], [])
    if not thread_builders:
        return None
    selected = _select_builders_to_dump(thread_builders)

    builder_io = []
    for bmeta in selected:
        tid = bmeta["trace_id"]
        if not tid:
            continue
        runs = list(client.list_runs(project_name=PROJECT, trace_id=tid))
        builder_run = next((r for r in runs if str(r.id) == str(tid)), None)
        if not builder_run:
            continue
        tool_children = [r for r in runs if r.run_type == "tool"]
        builder_io.append(_serialize_builder_io(builder_run, tool_children))

    return {
        "thread_id": thread["thread_id"],
        "violation_codes": thread["violation_codes"],
        "builder_count": thread["builder_count"],
        "builders_dumped": len(builder_io),
        "fail_count": thread["fail_count"],
        "first_start": thread["first_start"],
        "builders": builder_io,
    }


# ───────────────────────────────────────────────────── prompts ──

NARRATE_SYSTEM = """\
You analyze production traces from n8n's Instance AI workflow builder agent.
The agent builds or modifies n8n workflows by writing TypeScript files in a
sandbox, running `tsc`, and submitting via `submit-workflow`.

For each thread you receive: violation codes our heuristics flagged, then a
chronological dump of one or more builder sub-agent invocations. Each builder
shows TASK (orchestrator's instruction), TOOLS (the sub-agent's tool calls
with truncated inputs/outputs), and RESULT (final output or error).

Your job: explain WHAT WENT WRONG in plain language, in one short narrative
per thread. Be specific — name files, node types, error messages where they
appear. Avoid restating violation codes; explain the underlying behavior.

Use the `record_narrative` tool to return your answer. Confidence is "low"
when the trace is ambiguous or truncated heavily.
"""

NARRATE_TOOL = {
    "name": "record_narrative",
    "description": "Record the structured failure narrative for the thread.",
    "input_schema": {
        "type": "object",
        "properties": {
            "user_intent": {"type": "string", "description": "One sentence: what the user was trying to build/change."},
            "builder_strategy": {"type": "string", "description": "One sentence: what the builder agent attempted."},
            "failure_narrative": {"type": "string", "description": "2-4 sentences explaining what went wrong, in causal order. Cite specific file/tool/error names from the trace."},
            "pattern_tag": {"type": "string", "description": "Short kebab-case tag, 2-5 words, e.g. tsc-import-loop, missing-credentials, oversized-system-prompt, hallucinated-node-type."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["user_intent", "builder_strategy", "failure_narrative", "pattern_tag", "confidence"],
    },
}

CLUSTER_SYSTEM = """\
You receive a list of failure narratives (one per thread). Cluster them into
5-10 named themes. Each theme groups narratives that share a root cause or
symptom — not just surface keywords. A theme with 1 example is fine if it's
genuinely distinct.

Use the `record_themes` tool. Each theme has a short name, a 1-2 sentence
description of the underlying problem, the list of pattern_tag values that
fall under it, and example thread_ids (up to 5).
"""

CLUSTER_TOOL = {
    "name": "record_themes",
    "description": "Record clustered themes across all narratives.",
    "input_schema": {
        "type": "object",
        "properties": {
            "themes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "pattern_tags": {"type": "array", "items": {"type": "string"}},
                        "example_thread_ids": {"type": "array", "items": {"type": "string"}},
                        "count": {"type": "integer"},
                    },
                    "required": ["name", "description", "pattern_tags", "example_thread_ids", "count"],
                },
            },
        },
        "required": ["themes"],
    },
}


def build_thread_prompt(thread_io):
    parts = [
        f"THREAD: {thread_io['thread_id']}",
        f"violation_codes: {thread_io['violation_codes']}",
        f"builder_count: {thread_io['builder_count']}, failed: {thread_io['fail_count']}",
        "",
    ]
    for i, b in enumerate(thread_io["builders"], 1):
        parts.append(f"=== BUILDER {i} (trace {b['trace_id'][:8]}, status={b['status']}/{b['final_status']}) ===")
        parts.append(f"TASK: {b['task']}")
        if b["tools"]:
            parts.append(f"TOOLS ({len(b['tools'])} calls):")
            for j, t in enumerate(b["tools"], 1):
                parts.append(f"  [{j}] {t['name']}")
                if t["input"]:
                    parts.append(f"      input: {t['input']}")
                if t["output"]:
                    parts.append(f"      output: {t['output']}")
                if t["error"]:
                    parts.append(f"      ERROR: {t['error']}")
        parts.append(f"RESULT ({b['result_status']}): {b['result'] or b['error'] or '(empty)'}")
        parts.append("")
    return "\n".join(parts)


# ─────────────────────────────────────────── LLM orchestration ──

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
async def narrate_one(anthropic, sem, thread_io):
    async with sem:
        prompt = build_thread_prompt(thread_io)
        msg = await anthropic.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=NARRATE_SYSTEM,
            tools=[NARRATE_TOOL],
            tool_choice={"type": "tool", "name": "record_narrative"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in msg.content:
            if block.type == "tool_use" and block.name == "record_narrative":
                return {
                    "thread_id": thread_io["thread_id"],
                    "violation_codes": thread_io["violation_codes"],
                    "narrative": block.input,
                    "input_tokens": msg.usage.input_tokens,
                    "output_tokens": msg.usage.output_tokens,
                }
        raise RuntimeError(f"no tool_use for {thread_io['thread_id']}")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
async def cluster_themes(anthropic, narratives):
    payload = "\n\n".join(
        f"thread_id={n['thread_id']}\npattern_tag={n['narrative']['pattern_tag']}\nfailure: {n['narrative']['failure_narrative']}"
        for n in narratives
    )
    msg = await anthropic.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=CLUSTER_SYSTEM,
        tools=[CLUSTER_TOOL],
        tool_choice={"type": "tool", "name": "record_themes"},
        messages=[{"role": "user", "content": payload}],
    )
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"cluster step truncated at max_tokens, usage={msg.usage}")
    for block in msg.content:
        if block.type == "tool_use" and block.name == "record_themes":
            return block.input, msg.usage.input_tokens, msg.usage.output_tokens
    raise RuntimeError(f"no tool_use for cluster step (stop_reason={msg.stop_reason})")


# ──────────────────────────────────────────────────────── main ──

async def amain(args):
    classified_path = Path(args.classified_dir) / f"{args.window}.json"
    if not classified_path.exists():
        print(f"ERROR: {classified_path} not found. Run classify.py first.", file=sys.stderr)
        return 1
    classified = json.loads(classified_path.read_text())

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.window}.json"

    # Per-narrative cache lives separately so a crash mid-cluster doesn't
    # lose the expensive Opus calls.
    narr_cache_dir = Path(args.classified_dir).parent / "narrated_partial" / args.window
    narr_cache_dir.mkdir(parents=True, exist_ok=True)

    def narr_cache_path(thread_id):
        return narr_cache_dir / f"{thread_id}.json"

    cached = {}
    if not args.force:
        for f in narr_cache_dir.glob("*.json"):
            try:
                n = json.loads(f.read_text())
                cached[n["thread_id"]] = n
            except Exception:
                pass
        print(f"Loaded {len(cached)} cached narratives from {narr_cache_dir}")

    sample = select_sample(classified, args.sample_size)
    sample_ids = [t["thread_id"] for t in sample]
    print(f"Sample: {len(sample)} threads (cluster + outlier tail)")
    cluster_dist = Counter(tuple(sorted(t["violation_codes"])) for t in sample)
    print(f"Distinct violation-code tuples in sample: {len(cluster_dist)}")

    builders_by_thread = defaultdict(list)
    for b in classified["builders"]:
        if b.get("thread_id"):
            builders_by_thread[b["thread_id"]].append(b)

    # Refetch I/O for non-cached, in parallel.
    # Per-thread JSON cache so a crash doesn't lose finished work.
    io_cache_dir = Path(args.classified_dir).parent / "raw_io" / args.window
    io_cache_dir.mkdir(parents=True, exist_ok=True)

    def io_cache_path(thread_id):
        return io_cache_dir / f"{thread_id}.json"

    thread_ios = []
    needs_io = []
    for t in sample:
        if t["thread_id"] in cached:
            continue
        cp = io_cache_path(t["thread_id"])
        if cp.exists() and not args.force:
            try:
                io = json.loads(cp.read_text())
                if io and io.get("builders"):
                    thread_ios.append(io)
                    continue
            except Exception:
                pass
        needs_io.append(t)

    print(f"I/O cache: {len(thread_ios)} hits, {len(needs_io)} to fetch (concurrency={IO_FETCH_CONCURRENCY})")
    ls = LSClient()
    io_sem = asyncio.Semaphore(IO_FETCH_CONCURRENCY)
    done_count = [0]
    fail_count = [0]
    t0 = time.time()

    async def fetch_one(t):
        async with io_sem:
            try:
                io = await asyncio.to_thread(fetch_thread_io, ls, t, builders_by_thread)
                if io and io["builders"]:
                    io_cache_path(t["thread_id"]).write_text(json.dumps(io))
                    done_count[0] += 1
                    if done_count[0] % 10 == 0:
                        print(f"  {done_count[0]}/{len(needs_io)} ({time.time()-t0:.0f}s)")
                    return io
            except Exception as e:
                fail_count[0] += 1
                print(f"  ✗ I/O failed for {t['thread_id'][:8]}: {type(e).__name__}")
                return None

    results = await asyncio.gather(*(fetch_one(t) for t in needs_io), return_exceptions=False)
    thread_ios.extend(io for io in results if io and io["builders"])
    print(f"I/O done: {len(thread_ios)} threads ready, {fail_count[0]} failed, {time.time()-t0:.1f}s")

    if args.dry_run:
        # Dump intermediate for inspection
        dbg = out_dir / f"{args.window}.dryrun.json"
        dbg.write_text(json.dumps({"sample_size": len(sample), "thread_ios": thread_ios}, indent=2))
        print(f"Dry-run dump: {dbg}")
        return 0

    # Narrate in parallel; persist each narrative as soon as it completes
    # so cluster failures don't lose work.
    anthropic = AsyncAnthropic()
    sem = asyncio.Semaphore(args.max_concurrent)
    needs_narrate = [io for io in thread_ios if io["thread_id"] not in cached]
    print(f"\nNarrating {len(needs_narrate)} threads with {MODEL} (concurrency={args.max_concurrent})...")
    t0 = time.time()
    tasks = [narrate_one(anthropic, sem, io) for io in needs_narrate]
    new_narratives = []
    for fut in asyncio.as_completed(tasks):
        try:
            n = await fut
            new_narratives.append(n)
            narr_cache_path(n["thread_id"]).write_text(json.dumps(n))
            print(f"  ✓ {n['thread_id'][:8]}  tag={n['narrative']['pattern_tag']}  ({len(new_narratives)}/{len(tasks)})")
        except Exception as e:
            print(f"  ✗ failed: {type(e).__name__}: {e}")
    print(f"Narration done in {time.time()-t0:.1f}s")

    all_narratives = list(cached.values()) + new_narratives
    # Restrict to current sample
    all_narratives = [n for n in all_narratives if n["thread_id"] in set(sample_ids)]

    themes = []
    c_in = c_out = 0
    if all_narratives:
        print(f"\nClustering {len(all_narratives)} narratives into themes...")
        try:
            themes_data, c_in, c_out = await cluster_themes(anthropic, all_narratives)
            themes = themes_data.get("themes", [])
            if not themes:
                print(f"  ⚠ cluster step returned no themes: {themes_data}")
        except Exception as e:
            print(f"  ⚠ cluster step failed ({type(e).__name__}): {e}")

    total_in = sum(n.get("input_tokens", 0) for n in new_narratives) + c_in
    total_out = sum(n.get("output_tokens", 0) for n in new_narratives) + c_out

    blob = {
        "window": args.window,
        "model": MODEL,
        "narrated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(sample),
        "narratives": all_narratives,
        "themes": themes,
        "tokens": {"input": total_in, "output": total_out},
    }
    out_path.write_text(json.dumps(blob, indent=2))
    print(f"\nWrote {out_path}")
    print(f"Tokens: in={total_in:,}  out={total_out:,}")
    print(f"Themes: {len(themes)}")
    for th in themes:
        print(f"  • {th['name']} ({th['count']})")


def main():
    args = parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main() or 0)
