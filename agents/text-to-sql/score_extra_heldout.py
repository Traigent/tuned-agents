"""Score pre-registered candidate configurations on the 200 held-out questions (PAID Vertex AI calls).

usage: python score_extra_heldout.py <spend_limit_usd> <candidates.json> <label> [<label> ...]

  <candidates.json>  your pre-registered configs: {"label": {config...}, ...}. The archived run used
                     results/candidates.json; a fresh search needs its own file, written before this step.
Requires TRAIGENT_COST_APPROVED=true; the cap is min(argument, TRAIGENT_RUN_COST_LIMIT), each in (0, 10]. Writes <T2S_RESULTS_DIR or results-rerun>/heldout_<label>.json only if EVERY
label finished with no provider error, retry or timeout, gold_error, skipped (capped/fatal) or unpriced question; otherwise exits non-zero and writes nothing.
"""
import json, os, sys

if len(sys.argv) < 4:
    sys.exit(__doc__)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent"))
import paid_guard

try:
    paid_guard.arm_paid(float(sys.argv[1]))   # approval + min(CLI, env) cap + spend tracking, before any call
except (paid_guard.Refused, ValueError) as exc:
    sys.exit(f"REFUSING to score held-out: {exc}")
import run_optimization as R

OUT = os.environ.get("T2S_RESULTS_DIR", "results-rerun")
cands = json.load(open(sys.argv[2]))
results = {}
for label in sys.argv[3:]:
    r = R.score_heldout(label, cands[label], stop_on_bad=True)
    if paid_guard.is_aborted():
        sys.exit(f"ABORT: {label}: paid run aborted. {paid_guard.report()}. No score files written.")
    bad = [e["example_id"] for e in r["per_example"] if R.row_is_bad(e)]
    if bad or r.get("stopped_early") or R._FATAL["msg"] or R._SPEND["unpriced_rows"]:
        sys.exit(f"ABORT: {label}: {len(bad)} bad rows (error, skipped by cap or provider failure, gold_error, "
                 f"retry/timeout, unpriced); fatal: {R._FATAL['msg']}; unpriced rows {R._SPEND['unpriced_rows']}. "
                 "No score files written. " + paid_guard.report())
    total_label = sum(e["cost"] for e in r["per_example"])
    if abs(R._SPEND["total"] - sum(x["cost_total"] for x in results.values()) - total_label) > 1e-9:
        sys.exit(f"ABORT: {label}: reported cost does not match the spend the guard recorded call by call. No score files written.")
    results[label] = r
    print(label, r["accuracy"], r["cost_total"], r["cost_per_query"], r["latency_ms_mean"], r["errors"],
          "spend", R._SPEND["total"], flush=True)
os.makedirs(OUT, exist_ok=True)
for label, r in results.items():
    json.dump(r, open(f"{OUT}/heldout_{label}.json", "w"), indent=2, default=str)
