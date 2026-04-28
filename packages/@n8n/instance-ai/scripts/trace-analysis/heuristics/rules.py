"""Pure functions, one per heuristic.

Each function takes (builder_dict, thresholds) and returns either
None (no violation) or a dict {code, evidence}. Adding a new heuristic =
add a function and append it to ALL.
"""


def step_explosion(b, t):
    if b["llm_calls"] >= t.llm_calls:
        return {"code": "step_explosion", "evidence": {"llm_calls": b["llm_calls"], "threshold": t.llm_calls}}
    return None


def ts_error_loop(b, t):
    if b["exec_calls"] >= t.exec_calls:
        return {"code": "ts_error_loop", "evidence": {"exec_calls": b["exec_calls"], "threshold": t.exec_calls}}
    return None


def edit_thrash(b, t):
    if b["edit_calls"] >= t.edit_calls:
        return {"code": "edit_thrash", "evidence": {"edit_calls": b["edit_calls"], "threshold": t.edit_calls}}
    return None


def write_thrash(b, t):
    if b["write_calls"] >= t.write_calls:
        return {"code": "write_thrash", "evidence": {"write_calls": b["write_calls"], "threshold": t.write_calls}}
    return None


def submit_loop(b, t):
    if b["submit_calls"] >= t.submit_calls:
        return {"code": "submit_loop", "evidence": {"submit_calls": b["submit_calls"], "threshold": t.submit_calls}}
    return None


def latency_outlier(b, t):
    if b["latency_s"] is not None and b["latency_s"] >= t.latency_s:
        return {"code": "latency_outlier", "evidence": {"latency_s": round(b["latency_s"], 1), "threshold": t.latency_s}}
    return None


def token_outlier(b, t):
    if b["tokens"] is not None and b["tokens"] >= t.tokens:
        return {"code": "token_outlier", "evidence": {"tokens": b["tokens"], "threshold": t.tokens}}
    return None


def hard_failure(b, _t):
    if b["status"] == "error" or b["final_status"] == "failed":
        return {"code": "hard_failure", "evidence": {"status": b["status"], "final_status": b["final_status"]}}
    return None


def cancelled(b, _t):
    if b["final_status"] == "cancelled":
        return {"code": "cancelled", "evidence": {"final_status": b["final_status"]}}
    return None


ALL = [
    step_explosion,
    ts_error_loop,
    edit_thrash,
    write_thrash,
    submit_loop,
    latency_outlier,
    token_outlier,
    hard_failure,
    cancelled,
]


def evaluate(builder, thresholds):
    """Return a list of violations triggered by this builder."""
    violations = []
    for fn in ALL:
        v = fn(builder, thresholds)
        if v:
            violations.append(v)
    return violations


def thread_retry(thread_builders):
    """Per-thread heuristic: ≥2 builder invocations, ≥1 failed."""
    if len(thread_builders) < 2:
        return None
    failed = sum(1 for b in thread_builders if b["status"] == "error" or b["final_status"] in ("failed", "cancelled"))
    if failed >= 1:
        return {
            "code": "builder_retry_in_thread",
            "evidence": {"builder_count": len(thread_builders), "failed_count": failed},
        }
    return None
