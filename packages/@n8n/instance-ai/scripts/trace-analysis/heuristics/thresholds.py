"""Dynamic threshold computation with documented hard floors.

For each numeric metric we use the p95 of the current window, but never
below the hard floor. The floors were chosen from a one-time inspection
of a 7-day window (2026-04-21..2026-04-28) and are documented in
README.md so they don't drift silently.
"""

from dataclasses import dataclass


# Hard floors — minimum values for thresholds, regardless of window p95.
FLOOR_LLM_CALLS = 15
FLOOR_EXEC = 5
FLOOR_EDIT = 7
FLOOR_WRITE = 4
FLOOR_SUBMIT = 3
FLOOR_LATENCY_S = 600.0
FLOOR_TOKENS = 500_000


def percentile(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    i = max(0, min(len(s) - 1, int(p * len(s))))
    return s[i]


@dataclass
class Thresholds:
    llm_calls: int
    exec_calls: int
    edit_calls: int
    write_calls: int
    submit_calls: int
    latency_s: float
    tokens: int

    def to_dict(self):
        return {
            "llm_calls": self.llm_calls,
            "exec_calls": self.exec_calls,
            "edit_calls": self.edit_calls,
            "write_calls": self.write_calls,
            "submit_calls": self.submit_calls,
            "latency_s": self.latency_s,
            "tokens": self.tokens,
        }


def compute(builders):
    """Compute window thresholds from a list of builder dicts.

    Each builder dict needs: llm_calls, exec_calls, edit_calls, write_calls,
    submit_calls, latency_s, tokens.
    """

    def p95(key, floor):
        vals = [b[key] for b in builders if b.get(key) is not None]
        v = percentile(vals, 0.95) or 0
        return max(v, floor)

    return Thresholds(
        llm_calls=int(p95("llm_calls", FLOOR_LLM_CALLS)),
        exec_calls=int(p95("exec_calls", FLOOR_EXEC)),
        edit_calls=int(p95("edit_calls", FLOOR_EDIT)),
        write_calls=int(p95("write_calls", FLOOR_WRITE)),
        submit_calls=int(p95("submit_calls", FLOOR_SUBMIT)),
        latency_s=float(p95("latency_s", FLOOR_LATENCY_S)),
        tokens=int(p95("tokens", FLOOR_TOKENS)),
    )
