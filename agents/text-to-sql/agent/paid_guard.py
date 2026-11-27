"""One guard for every entry point that can spend money (paid Vertex AI calls).

Global rule for a paid (real) run: ANY provider exception, timeout, or reply of unknown cost calls
abort() at once; nothing is retried; every later provider call is refused; entry points exit non-zero and
print report() (attempted calls and known spend). Mock mode is unaffected.

Rules, enforced here and nowhere else:
  - a paid run needs TRAIGENT_COST_APPROVED=true;
  - the effective cap = min(command-line limit, TRAIGENT_RUN_COST_LIMIT), each of those that is set
    must be a number in (0, 10]; with neither set the cap is 10 USD;
  - spend tracking (SPEND) is armed BEFORE any provider call, preflight included;
  - agent.py asks `before_provider_call()` ahead of every provider call, so nothing can reach the
    provider unless the run was armed (paid) or declared mock.
Only the standard library is imported, so it is cheap to test.
"""
from __future__ import annotations

import os

MAX_CAP_USD = 10.0


class Refused(RuntimeError):
    """A paid action was refused (not approved, bad cap, cap reached, or not armed)."""


SPEND = {"total": 0.0, "limit": MAX_CAP_USD, "real": False, "capped": False,
         "unpriced_rows": 0, "rows": 0, "attempts": 0}
_STATE = {"armed": False, "aborted": None}


def effective_cap(cli_limit: float | None = None, env=None) -> tuple[float | None, list[str]]:
    """Return (cap, problems). Makes no provider call."""
    env = os.environ if env is None else env
    problems: list[str] = []
    if str(env.get("TRAIGENT_COST_APPROVED", "")).lower() != "true":
        problems.append("TRAIGENT_COST_APPROVED=true is required")
    limits: list[float] = []
    raw_env = env.get("TRAIGENT_RUN_COST_LIMIT")
    for name, raw in (("spend limit argument", cli_limit), ("TRAIGENT_RUN_COST_LIMIT", raw_env)):
        if raw is None or raw == "":
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            problems.append(f"{name} is not a number")
            continue
        if not (0 < v <= MAX_CAP_USD):
            problems.append(f"{name} must be in (0, {MAX_CAP_USD:g}]")
            continue
        limits.append(v)
    if problems:
        return None, problems
    return (min(limits) if limits else MAX_CAP_USD), []


def arm_paid(cli_limit: float | None = None, env=None) -> float:
    """Validate approval and caps, reset spend tracking, arm the run. Raises Refused."""
    cap, problems = effective_cap(cli_limit, env)
    if problems or cap is None:
        raise Refused("; ".join(problems))
    SPEND.update(total=0.0, limit=cap, real=True, capped=False, unpriced_rows=0, rows=0, attempts=0)
    _STATE.update(armed=True, aborted=None)
    # No SDK-side vendor retries in a paid run (the SDK reads this when the run starts).
    os.environ["TRAIGENT_VENDOR_MAX_RETRIES"] = "0"
    return cap


def arm_mock() -> None:
    """Mock mode: provider calls are intercepted by the SDK; nothing is billed."""
    SPEND.update(real=False)
    _STATE.update(armed=True, aborted=None)


def before_provider_call() -> None:
    if not _STATE["armed"]:
        raise Refused("provider call refused: run not armed (paid approval/cap not validated)")
    if _STATE["aborted"]:
        raise Refused(f"provider call refused: run aborted ({_STATE['aborted']})")
    if SPEND["real"] and SPEND["total"] >= SPEND["limit"]:
        SPEND["capped"] = True
        raise Refused(f"provider call refused: spend cap ${SPEND['limit']:g} reached")
    SPEND["attempts"] += 1


def is_aborted() -> str | None:
    return _STATE["aborted"]


def report() -> str:
    return (f"attempted provider calls: {SPEND['attempts']}; known spend: ${SPEND['total']:.6f} "
            f"(cap ${SPEND['limit']:g}); abort reason: {_STATE['aborted'] or 'none'}")


def usage_unknown(prompt_tokens, completion_tokens) -> bool:
    """Cost cannot be trusted: usage missing, or zero prompt tokens for a non-empty prompt."""
    return (prompt_tokens is None or completion_tokens is None
            or prompt_tokens <= 0 or completion_tokens < 0)


def abort(reason: str) -> None:
    """Stop all further provider calls in this process (e.g. a response whose cost cannot be measured)."""
    if _STATE["aborted"] is None:
        _STATE["aborted"] = reason


def record_cost(usd: float) -> None:
    """Called after EVERY provider call (retries, repair calls and preflight included)."""
    SPEND["total"] += usd
