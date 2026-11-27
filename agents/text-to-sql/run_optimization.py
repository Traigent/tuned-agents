#!/usr/bin/env python
"""Optimize the Spider text-to-SQL agent from the TVL spec.

  python run_optimization.py --mock    # offline, SDK mock LLM, no network, no spend
  python run_optimization.py --real    # PAID Vertex AI calls (cap $10); search only, held-out is scored separately

What comes FROM the TVL spec (spec/text2sql-spider.tvl.yml) via @traigent.optimize(tvl_spec=...):
configuration space, structural constraints, objectives (+ weights/orientation), the
evaluation dataset path (data/search.jsonl) and the promotion policy.
What is supplied IN CODE (and why): see the CODE-SUPPLIED block below.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)                                   # dataset paths in the spec are relative
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("TRAIGENT_DATASET_ROOT", str(ROOT))
# SDK-side cost accounting must use OUR prices: litellm 1.111's bundled map returns 0 for these ids.
os.environ["TRAIGENT_CUSTOM_MODEL_PRICING_FILE"] = str(ROOT / "agent" / "model_pricing.json")

import paid_guard  # noqa: E402
import traigent  # noqa: E402  (import BEFORE litellm: sets LITELLM_LOCAL_MODEL_COST_MAP)
from traigent.api.decorators import EvaluationOptions, ExecutionOptions  # noqa: E402
from traigent.config.context import set_config  # noqa: E402
from traigent.api.types import ExampleResult  # noqa: E402

import agent as A  # noqa: E402
from sqlutil import TIMEOUT, exec_match  # noqa: E402

SPEC = ROOT / "spec" / "text2sql-spider.tvl.yml"
HELDOUT = Path(os.environ.get("T2S_HELDOUT", ROOT / "data" / "heldout.jsonl"))   # override is for tests
MAX_COST_CAP_USD = paid_guard.MAX_CAP_USD
REAL_TRIALS = 25

# --- CODE-SUPPLIED (not expressible in the spec) --------------------------------------
# The baseline ("default config") for the held-out comparison: the spec schema has no
# per-tvar default (tvl.schema.json TVarDecl additionalProperties=false; SDK loader returns
# default_config == {}), so it is declared here. It satisfies every spec constraint.
BASELINE = {"model": "vertex_ai/gemini-3.5-flash-lite", "temperature": 0.0, "fewshot_k": 0,
            "fewshot_selector": "fixed", "generation_path": "direct",
            "schema_context": "full_ddl_fk", "repair": "off", "output_mode": "sql_only"}
# The evaluator (execution match + real cost/latency/timeout metrics) and trial count/algorithm
# are code by design: TVL declares metric_refs, the harness resolves them.
# -------------------------------------------------------------------------------------

_SPEND = paid_guard.SPEND   # shared with agent.py through paid_guard


_FATAL = {"msg": None}          # set on a terminal provider error (auth / permission): stop spending
_ROWS: list[dict] = []          # per-example records of this process (partial-results source)
_ROWS_FILE = {"path": None}     # results/example_rows.jsonl, appended per example (survives a crash)
_KEYS = ["model", "temperature", "fewshot_k", "fewshot_selector", "generation_path",
         "schema_context", "repair", "output_mode"]


def _report_cost_to_sdk(slot, pred, st) -> None:
    """Tell the SDK this example's agent reported its own TOTAL cost (traigent.with_usage).

    Why: the SDK's vendor-attempt ledger (traigent/utils/vendor_retry.py:504-541, applied at
    core/trial_lifecycle.py:1020-1045) records every provider attempt; a FAILED attempt has no
    usage, so it counts as "unmeasured" and flags the whole trial cost_unmeasured, which under
    strict cost accounting aborts the run (core/cost_enforcement.py:1589). An agent-reported
    total covers the example's attempts (vendor_retry.py:544-559). We report ONLY when it is
    true: every failed attempt was an HTTP error response from the provider (no completion,
    not billed = known $0) and every successful call had priced usage. A timeout/dropped
    connection (maybe billed) or a missing-usage success is NOT reported -> stays unknown.
    """
    from traigent.utils.vendor_retry import mark_if_agent_reported_cost
    if slot is None or st["unpriced"] or st["unknown_attempts"]:
        return
    out = traigent.with_usage(pred, total_cost=st["cost"], input_tokens=st["prompt_tokens"],
                              output_tokens=st["completion_tokens"])
    if not (isinstance(out, dict) and "__traigent_meta__" in out):
        raise RuntimeError("traigent.with_usage did not return usage metadata inside the "
                           "evaluator; cannot report this example's cost to the SDK")
    mark_if_agent_reported_cost(slot, out)


def exec_eval(func, config, example) -> ExampleResult:
    """Execution-match accuracy + real cost (USD/query) + latency (ms) + timeout flag.

    cost = provider-reported token usage x model_pricing.json, summed over ALL attempts.
    A call that returned no completion and was not billed is cost 0 / 0 tokens AND a failure
    (accuracy 0, success=False); a possibly-billed failure is never reported as 0.
    """
    from traigent.utils.langchain_interceptor import example_slot
    inp = example.input_data
    question = inp["input"] if isinstance(inp, dict) else inp
    gold = example.expected_output
    md = example.metadata or {}
    db_id = md.get("db_id") or (inp.get("db_id") if isinstance(inp, dict) else None)
    ex_id = str(md.get("example_id") or md.get("id") or question)
    A.CALL.reset()
    t0 = time.time()
    pred, acc, status, success, err = "", 0.0, "error", True, None
    with example_slot() as slot:
        if _FATAL["msg"]:
            success, err, status = False, f"skipped, no call made: {_FATAL['msg']}", "fatal"
        elif _SPEND["real"] and _SPEND["total"] >= _SPEND["limit"]:
            _SPEND["capped"] = True                      # hard stop: no further model calls
            success, err, status = False, f"cost cap ${_SPEND['limit']} reached", "capped"
        else:
            try:
                pred = func(question, db_id)
                acc, status = exec_match(pred, gold, db_id)
            except Exception as exc:  # provider/parse errors are FAILURES, not declines
                pred, acc, success, err, status = "", 0.0, False, f"{type(exc).__name__}: {exc}"[:300], "exception"
                if paid_guard.is_aborted() and not _FATAL["msg"]:
                    _FATAL["msg"] = f"paid run aborted: {paid_guard.is_aborted()}"
                cause = exc.__cause__ or exc
                if getattr(cause, "status_code", None) in (401, 402, 403) and not _FATAL["msg"]:
                    # terminal: bad/expired credentials, missing IAM role or billing/quota denial. Every further call would fail
                    # the same way; stop making calls and let main() exit nonzero with partial results.
                    _FATAL["msg"] = (f"provider rejected the credentials (HTTP {cause.status_code}): "
                                     f"{str(cause)[:240]}")
                    print(f"[FATAL] {_FATAL['msg']}", flush=True)
        st = A.CALL.get()
        _report_cost_to_sdk(slot, pred, st)
    _SPEND["rows"] += 1
    if success and st["unpriced"] and _SPEND["real"]:
        success, err = False, "cost could not be measured: model has no price"
        _SPEND["unpriced_rows"] += 1
    elapsed = time.time() - t0
    row = {"example_id": ex_id, "config": {k: config.get(k) for k in _KEYS}, "status": status,
           "success": success, "error": err, "accuracy": acc, "cost": st["cost"],
           "latency_ms": st["latency_ms"] or elapsed * 1000.0, "attempts": st["attempts"],
           "unknown_attempts": st["unknown_attempts"], "initial_status": st["initial_status"] or status,
           "correct": acc == 1.0, "prompt_tokens": st["prompt_tokens"],
           "completion_tokens": st["completion_tokens"], "last_error": st["last_error"]}
    _ROWS.append(row)
    if _ROWS_FILE["path"]:
        with open(_ROWS_FILE["path"], "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    return ExampleResult(
        example_id=ex_id, input_data=inp if isinstance(inp, dict) else {"input": question},
        expected_output=gold, actual_output=pred,
        metrics={"accuracy": acc, "cost": st["cost"], "latency": st["latency_ms"] or elapsed * 1000.0,
                 "timeout_rate": 1.0 if status == TIMEOUT else 0.0},
        execution_time=elapsed, success=success, error_message=err,
        metadata={"exec_status": status, "calls": st["calls"], "attempts": st["attempts"],
                  "unknown_attempts": st["unknown_attempts"], "initial_status": st["initial_status"] or status,
                  "prompt_tokens": st["prompt_tokens"], "completion_tokens": st["completion_tokens"]},
    )


def write_partial(outdir: Path, reason: str, extra: dict) -> None:
    """The SDK raised mid-run: write what completed from our own per-example records."""
    groups: dict[str, list[dict]] = {}
    for r in _ROWS:
        groups.setdefault(json.dumps(r["config"], sort_keys=True), []).append(r)
    with (outdir / "trials.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["PARTIAL", "n_examples", "n_failed", *_KEYS, "accuracy", "cost_per_query",
                    "cost_total", "latency_ms_mean", "timeout_rate"])
        for k, rows in groups.items():
            cfg, n = json.loads(k), len(rows)
            w.writerow(["complete" if n >= 100 else "incomplete", n, sum(1 for r in rows if not r["success"]),
                        *[cfg[x] for x in _KEYS], sum(r["accuracy"] for r in rows) / n,
                        sum(r["cost"] for r in rows) / n, sum(r["cost"] for r in rows),
                        sum(r["latency_ms"] for r in rows) / n,
                        sum(1 for r in rows if r["status"] == TIMEOUT) / n])
    summary = {"PARTIAL": True, "partial_reason": reason, "total_spend_usd": round(_SPEND["total"], 6),
               "rows_scored": _SPEND["rows"], "unpriced_rows": _SPEND["unpriced_rows"],
               "configs_seen": len(groups),
               "configs_complete": sum(1 for g in groups.values() if len(g) >= 100), **extra}
    (outdir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))


def spec_constraints(art):
    """Structural constraints compiled from the spec (pre-trial filters).

    The spec's `constraints.derived` is NOT handed to the SDK (CODE-SUPPLIED routing):
    the SDK loader compiles each derived `require` as a per-trial Python expression over
    (params, metrics) (traigent/tvl/spec_loader.py:1085-1100), so the tvl-valid
    `env.context.*` form raises NameError at trial time (verified in mock). It is checked by
    tvl-check-operational instead. The per-query budget is NOT added as a post-trial metric
    constraint either: verified in mock that one over-budget trial aborts the WHOLE run
    (CostTrackingRequiredError, traigent/core/cost_enforcement.py:1590-1735). Budget is
    enforced by TRAIGENT_RUN_COST_LIMIT + the spend cap in exec_eval, and reported against
    the spec's query_budget_usd in run_summary.json.
    """
    n_derived = len(art.derived_constraints or [])
    return list(art.constraints[: len(art.constraints) - n_derived])


EXPERIMENT_NAME = "tvl-spider-text2sql"   # traigent.optimize(experiment_name=...) is the supported way


def build(offline: bool, art, experiment_name: str = EXPERIMENT_NAME):
    from traigent.tvl.options import TVLOptions
    return traigent.optimize(
        tvl=TVLOptions(spec_path=str(SPEC), apply_constraints=False),  # space, objectives, dataset, budget(trials) from spec
        constraints=spec_constraints(art),
        default_config=BASELINE,
        evaluation=EvaluationOptions(custom_evaluator=exec_eval),
        execution=ExecutionOptions(offline=offline),
        experiment_name=experiment_name,
    )(A.generate_sql)


# A model query that fails to execute or times out in SQLite is a WRONG ANSWER (scored incorrect), not a harness failure.
_SQL = {"error": "sql_error", "timeout": "sql_timeout"}


def row_is_bad(e: dict) -> bool:
    """A held-out row that must not be silently scored: any error field, skipped (cap/fatal) or
    exception row, gold_error, a provider retry (attempts > calls) or a possibly-billed failed attempt."""
    return bool(e.get("error") is not None or e["status"] in ("capped", "fatal", "exception", "gold_error")
                or e.get("attempts", 0) != e.get("calls", 0) or e.get("unknown_attempts", 0))


def score_heldout(label: str, config: dict, stop_on_bad: bool = False) -> dict:
    """Score one config on the held-out rows the search never saw."""
    rows = [json.loads(line) for line in HELDOUT.read_text().splitlines()]
    set_config(dict(config))
    out = {"label": label, "config": config, "n": len(rows), "accuracy": 0.0, "cost_total": 0.0,
           "latency_ms_mean": 0.0, "timeouts": 0, "errors": 0, "per_example": []}
    for r in rows:
        class _Ex:  # same shape the SDK hands exec_eval
            input_data = {"input": r["input"]}
            expected_output = r["output"]
            metadata = {"db_id": r["db_id"], "example_id": r["example_id"]}
        res = exec_eval(A.generate_sql, config, _Ex)
        m = res.metrics
        out["accuracy"] += m["accuracy"]; out["cost_total"] += m["cost"]
        out["latency_ms_mean"] += m["latency"]; out["timeouts"] += int(m["timeout_rate"])
        out["errors"] += 0 if res.success else 1
        out["per_example"].append({"example_id": r["example_id"], "acc": m["accuracy"], "cost": m["cost"],
                                   "status": res.metadata["exec_status"], "error": res.error_message,
                                   "correct": m["accuracy"] == 1.0,
                                   "sql_status": _SQL.get(res.metadata["exec_status"], res.metadata["exec_status"]),
                                   "initial_sql_status": _SQL.get(res.metadata["initial_status"], res.metadata["initial_status"]),
                                   "calls": res.metadata["calls"], "attempts": res.metadata["attempts"],
                                   "unknown_attempts": res.metadata["unknown_attempts"]})
        if stop_on_bad and row_is_bad(out["per_example"][-1]):
            out["stopped_early"] = True
            break
    n = len(out["per_example"])
    out["accuracy"] /= n; out["latency_ms_mean"] /= n
    out["cost_per_query"] = out["cost_total"] / n
    return out


def write_trials(result, path: Path) -> None:
    keys = ["model", "temperature", "fewshot_k", "fewshot_selector", "generation_path",
            "schema_context", "repair", "output_mode"]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trial_id", "status", *keys, "accuracy", "cost", "latency", "timeout_rate", "score", "error"])
        for t in result.trials:
            m = t.metrics or {}
            w.writerow([t.trial_id, getattr(t.status, "value", t.status), *[t.config.get(k) for k in keys],
                        m.get("accuracy"), m.get("cost"), m.get("latency"), m.get("timeout_rate"),
                        t.score, t.error_message])


def summarize(result, outdir: Path, extra: dict) -> dict:
    md = getattr(result, "metadata", None) or {}
    import yaml
    budget = float(yaml.safe_load(SPEC.read_text())["environment"]["context"]["query_budget_usd"])
    # The SDK's strict evidence mode can WITHHOLD promotion (best_config == {} / best_score None,
    # e.g. "insufficient samples for statistical comparison"). Never pretend that is a winner:
    # fall back to the best search trial (accuracy desc, cost asc) and label it as such.
    best_config, best_source = getattr(result, "best_config", None) or {}, "sdk_promoted"
    if not best_config:
        done = [t for t in result.trials if getattr(t.status, "value", t.status) == "completed"
                and (t.metrics or {}).get("accuracy") is not None]
        if done:
            top = sorted(done, key=lambda t: (-t.metrics["accuracy"], t.metrics.get("cost", 0.0)))[0]
            best_config, best_source = dict(top.config), "search_best_trial_NOT_gate_promoted"
        else:
            best_source = "none"
    best_cost = None
    for t in result.trials:
        if t.config == best_config and (t.metrics or {}).get("cost") is not None:
            best_cost = t.metrics["cost"]
    summary = {
        "metadata_keys": sorted(md),
        "query_budget_usd": budget, "best_config_cost_per_query": best_cost,
        "best_config_within_budget": (best_cost is not None and best_cost <= budget),
        "best_score": getattr(result, "best_score", None),
        "best_config": best_config, "best_config_source": best_source,
        "promotion_note": md.get("promotion_note") or md.get("promotion_withheld_reason"),
        "n_trials": len(result.trials),
        "n_completed": sum(1 for t in result.trials if getattr(t.status, "value", t.status) == "completed"),
        "n_pruned_invalid_config": sum(1 for t in result.trials if getattr(t.status, "value", t.status) == "pruned"),
        "metadata": {k: md.get(k) for k in ("source", "fallback_reason", "persistence_rejection_reason", "persistence_status", "stop_detail")},
        "total_spend_usd": round(_SPEND["total"], 6),
        "rows_scored": _SPEND["rows"], "unpriced_rows": _SPEND["unpriced_rows"],
        "cap_hit": _SPEND["capped"], **extra,
    }
    (outdir / "best_config.json").write_text(json.dumps(summary["best_config"], indent=2, default=str))
    (outdir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--mock", action="store_true", help="offline, mock LLM, no spend")
    g.add_argument("--real", action="store_true", help="real Vertex AI calls, PAID (cap $10)")
    ap.add_argument("--mock-trials", type=int, default=5)
    ap.add_argument("--max-trials", type=int, default=REAL_TRIALS, help="--real trial count (default 25)")
    args = ap.parse_args()

    from traigent.tvl.spec_loader import load_tvl_spec
    art = load_tvl_spec(spec_path=SPEC)
    models = art.configuration_space["model"]
    print(f"[spec] loaded {SPEC.name}: tvars={list(art.configuration_space)} "
          f"constraints={len(art.constraints)} objectives={[o.name for o in art.objective_schema.objectives]} "
          f"dataset={art.evaluation_set.dataset}", flush=True)

    if args.mock:
        os.environ["TRAIGENT_OFFLINE_MODE"] = "true"
        # mock only: skip the SDK's pre-run cost preflight (needs a non-production ENVIRONMENT)
        os.environ.setdefault("ENVIRONMENT", "development")
        if not os.environ.get("T2S_KEEP_PREFLIGHT"):   # set to see the real preflight verdict offline
            os.environ["TRAIGENT_COST_PREFLIGHT_MOCK_EXEMPT"] = "true"
        from traigent.testing import enable_mock_mode_for_quickstart
        enable_mock_mode_for_quickstart()
        paid_guard.arm_mock()
        outdir = ROOT / "results-mock"; outdir.mkdir(exist_ok=True)
        offline, algorithm, trials = True, "random", args.mock_trials
    else:
        try:
            limit = paid_guard.arm_paid()    # approval + cap + spend tracking, BEFORE any provider call
        except paid_guard.Refused as exc:
            print(f"REFUSING to start --real: {exc}"); return 2
        problems = []
        # Vertex preflight: ADC auth works + one tiny real call (a few tokens) per model.
        problems += [f"Vertex preflight failed: {p}" for p in A.vertex_preflight(models)]
        unpriced = A.unpriced_models(models)
        if unpriced:
            problems.append(f"models without a positive per-token price: {unpriced}")
        if problems or paid_guard.is_aborted():
            print("REFUSING to start --real:\n  - " + "\n  - ".join(problems)); print("[guard] " + paid_guard.report()); return 2
        os.environ["TRAIGENT_RUN_COST_LIMIT"] = str(limit)
        os.environ["TRAIGENT_OFFLINE_MODE"] = "true"   # no Traigent backend is needed; calls to Vertex are real
        outdir = Path(os.environ.get("T2S_RESULTS_DIR", ROOT / "results-rerun")); outdir.mkdir(exist_ok=True)
        offline, algorithm, trials = True, "auto", args.max_trials

    opt = build(offline, art)
    print(f"[run] mode={'MOCK' if args.mock else 'REAL'} offline={offline} algorithm={algorithm} "
          f"max_trials={trials} cost_limit={_SPEND['limit'] if args.real else 'n/a'}", flush=True)
    _ROWS_FILE["path"] = outdir / "example_rows.jsonl"
    _ROWS_FILE["path"].write_text("")
    try:
        result = opt.optimize_sync(max_trials=trials, algorithm=algorithm)
    except Exception as exc:       # keep the completed work, then fail loudly
        if args.real:
            print("[guard] " + paid_guard.report())
        reason = f"{type(exc).__name__}: {exc}"[:400]
        write_partial(outdir, reason, {"mode": "mock" if args.mock else "real", "max_trials": trials})
        print(f"[PARTIAL] run aborted: {reason}\n[PARTIAL] wrote {outdir}/trials.csv and run_summary.json "
              f"(spend ${_SPEND['total']:.4f}); per-example rows in {outdir}/example_rows.jsonl", flush=True)
        return 3
    if args.real and (paid_guard.is_aborted() or _FATAL["msg"]):
        print("[guard] " + paid_guard.report())
    if _FATAL["msg"]:
        write_partial(outdir, "FATAL: " + _FATAL["msg"], {"mode": "real", "max_trials": trials})
        print(f"[PARTIAL] run finished but a terminal provider error stopped all calls: {_FATAL['msg']}", flush=True)
        return 4
    write_trials(result, outdir / "trials.csv")
    summary = summarize(result, outdir, {"mode": "mock" if args.mock else "real", "max_trials": trials})
    print("[result] best_score=", summary["best_score"], " best_config=", summary["best_config"],
          " source=", summary["best_config_source"])
    print("[result] trials=", summary["n_trials"], "completed=", summary["n_completed"], "pruned=", summary["n_pruned_invalid_config"],
          "spend_usd=", summary["total_spend_usd"],
          "metadata=", summary["metadata"], flush=True)

    if args.real:
        print("[done] search finished. Held-out is NOT scored here: freeze your pre-registration "
              "first, then run score_extra_heldout.py (see README).", flush=True)
        if _SPEND["unpriced_rows"]:
            print("[ERROR] unpriced rows seen; cost objective not trustworthy"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
