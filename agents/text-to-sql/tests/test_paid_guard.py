"""Tests for the paid-call guard. A stub provider replaces litellm; no network is used.
Run from the repo root:  python -m unittest tests.test_paid_guard -v
(The end-to-end cases need Spider: run scripts/get_spider.sh or set SPIDER_DIR; they skip otherwise.)"""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
import paid_guard  # noqa: E402

OK_ENV = {"TRAIGENT_COST_APPROVED": "true"}


def run(args, extra_env, log, mode="ok"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TRAIGENT_COST_APPROVED", "TRAIGENT_RUN_COST_LIMIT", "T2S_RESULTS_DIR")}
    env.update(extra_env)
    env.update(PYTHONPATH=str(ROOT / "tests" / "stubs"), STUB_LOG=str(log), STUB_MODE=mode)
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)


def spider_ok():
    d = Path(os.environ.get("SPIDER_DIR", ROOT / "spider_data"))
    return (d / "dev.json").exists() and (ROOT / "data" / "heldout.jsonl").exists()


class CapRules(unittest.TestCase):
    def test_smaller_of_cli_and_env_wins(self):
        env = {**OK_ENV, "TRAIGENT_RUN_COST_LIMIT": "3"}
        self.assertEqual(paid_guard.effective_cap(5, env), (3.0, []))
        env = {**OK_ENV, "TRAIGENT_RUN_COST_LIMIT": "7"}
        self.assertEqual(paid_guard.effective_cap(2, env), (2.0, []))

    def test_default_cap_is_ten(self):
        self.assertEqual(paid_guard.effective_cap(None, OK_ENV), (10.0, []))

    def test_refusals(self):
        for cli, env in [(5, {}), (11, OK_ENV), (0, OK_ENV), (-1, OK_ENV),
                         (5, {**OK_ENV, "TRAIGENT_RUN_COST_LIMIT": "50"}),
                         (5, {**OK_ENV, "TRAIGENT_RUN_COST_LIMIT": "abc"}),
                         (5, {"TRAIGENT_COST_APPROVED": "yes"})]:
            cap, problems = paid_guard.effective_cap(cli, env)
            self.assertIsNone(cap, (cli, env))
            self.assertTrue(problems)

    def test_unarmed_provider_call_is_refused(self):
        paid_guard._STATE["armed"] = False
        with self.assertRaises(paid_guard.Refused):
            paid_guard.before_provider_call()


class EntryPointsRefuse(unittest.TestCase):
    def check(self, args):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.log"
            r = run(args, {}, log)
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("REFUSING", r.stdout + r.stderr)
            self.assertFalse(log.exists(), "the stub provider was called although the run was refused")

    def test_run_optimization_real(self):
        self.check(["run_optimization.py", "--real"])

    def test_score_extra_heldout(self):
        self.check(["score_extra_heldout.py", "5", "results/candidates.json", "baseline"])

    def test_rerun_baseline_failures(self):
        self.check(["scripts/rerun_baseline_failures.py"])


@unittest.skipUnless(spider_ok(), "needs Spider (scripts/get_spider.sh)")
class ScoringAbortsWithoutWritingFiles(unittest.TestCase):
    def check(self, extra_env, mode, calls_expected=None, cfg_patch=None, heldout_rows=None):
        import json
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out, log = d / "out", d / "calls.log"
            cands = json.loads((ROOT / "results" / "candidates.json").read_text())
            cands["baseline"].update(cfg_patch or {})
            (d / "cands.json").write_text(json.dumps(cands))
            env = {**OK_ENV, "T2S_RESULTS_DIR": str(out), **extra_env}
            if heldout_rows:
                (d / "held.jsonl").write_text("\n".join(json.dumps(r) for r in heldout_rows) + "\n")
                env["T2S_HELDOUT"] = str(d / "held.jsonl")
            r = run(["score_extra_heldout.py", "5", str(d / "cands.json"), "baseline"], env, log, mode)
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("ABORT", r.stdout + r.stderr)
            self.assertFalse(out.exists() and any(out.iterdir()), "score files were written")
            n = len(log.read_text().splitlines()) if log.exists() else 0
            if calls_expected is not None:
                self.assertEqual(n, calls_expected, f"provider calls: {n}")

    def test_provider_error_rows_abort(self):
        self.check({}, "err400")

    def test_env_cap_smaller_than_cli_cap_stops_spending(self):
        # one stub call costs ~$0.00055; env cap 0.001 (< CLI 5) is hit after two calls
        self.check({"TRAIGENT_RUN_COST_LIMIT": "0.001"}, "ok", calls_expected=2)

    def test_cap_crossed_by_first_call_makes_no_repair_call(self):
        # the first call ($0.00055) crosses the $0.0005 cap; the repair call must not happen
        self.check({"TRAIGENT_RUN_COST_LIMIT": "0.0005"}, "bad_sql", calls_expected=1, cfg_patch={"repair": "on"})

    def test_unpriced_response_stops_all_calls(self):
        self.check({}, "nousage", calls_expected=1)

    def test_timeout_then_success_aborts(self):
        self.check({}, "timeout_once", calls_expected=1)   # no retry in a paid run: abort at the first timeout

    def test_gold_error_aborts(self):
        row = {"input": "x", "output": "SELECT nope FROM nowhere", "db_id": "car_1", "example_id": "t-1"}
        self.check({}, "ok", heldout_rows=[row])


class PreflightAndRerunAccounting(unittest.TestCase):
    def test_unpriced_preflight_reply_aborts_all_further_calls(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.log"
            r = run(["run_optimization.py", "--real"], dict(OK_ENV), log, "nousage")
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("REFUSING", r.stdout + r.stderr)
            self.assertEqual(len(log.read_text().splitlines()), 1, "calls continued after an unpriced preflight reply")

    def test_zero_prompt_tokens_reply_is_unknown_cost_and_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.log"
            r = run(["run_optimization.py", "--real"], dict(OK_ENV), log, "zero_prompt")
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_paid_arming_disables_sdk_vendor_retries(self):
        old = dict(os.environ)
        try:
            os.environ["TRAIGENT_VENDOR_MAX_RETRIES"] = "3"
            paid_guard.arm_paid(5, OK_ENV)
            self.assertEqual(os.environ["TRAIGENT_VENDOR_MAX_RETRIES"], "0")
        finally:
            os.environ.clear(); os.environ.update(old)
            paid_guard.SPEND["real"] = False; paid_guard._STATE.update(armed=False, aborted=None)

    def test_preflight_timeout_aborts_after_one_call(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.log"
            r = run(["run_optimization.py", "--real"], dict(OK_ENV), log, "timeout_always")
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(len(log.read_text().splitlines()), 1)
            self.assertIn("attempted provider calls: 1", r.stdout + r.stderr)

    @unittest.skipUnless(spider_ok(), "needs Spider (scripts/get_spider.sh)")
    def test_rerun_timeout_aborts_nonzero_with_no_further_calls(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.log"
            r = run(["scripts/rerun_baseline_failures.py"], dict(OK_ENV), log, "timeout_always")
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(len(log.read_text().splitlines()), 1)
            self.assertIn("attempted provider calls: 1", r.stdout + r.stderr)

    @unittest.skipUnless(spider_ok(), "needs Spider (scripts/get_spider.sh)")
    def test_rerun_script_counts_each_call_once(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.log"
            r = run(["scripts/rerun_baseline_failures.py"], dict(OK_ENV), log)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            n = len(log.read_text().splitlines())
            self.assertGreater(n, 0)
            spend = float(r.stderr.split("spend recorded by the guard: $")[1].split()[0])
            self.assertAlmostEqual(spend, n * 0.00055, places=6)


@unittest.skipUnless(spider_ok(), "needs Spider (scripts/get_spider.sh)")
class SqlFailuresAreScoredWrongAndVisible(unittest.TestCase):
    def test_model_sql_error_is_scored_incorrect_and_repair_keeps_initial_status(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); out = d / "out"
            cands = json.loads((ROOT / "results" / "candidates.json").read_text())
            cands["baseline"]["repair"] = "on"      # stub always answers a query that does not execute
            (d / "c.json").write_text(json.dumps(cands))
            rows = [{"input": "How many cars?", "output": "SELECT count(*) FROM cars_data", "db_id": "car_1", "example_id": "t-1"}]
            (d / "h.jsonl").write_text(json.dumps(rows[0]) + "\n")
            r = run(["score_extra_heldout.py", "5", str(d / "c.json"), "baseline"],
                    {**OK_ENV, "T2S_RESULTS_DIR": str(out), "T2S_HELDOUT": str(d / "h.jsonl")}, d / "log", "bad_sql")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # a wrong model query is a result, not an abort
            e = json.loads((out / "heldout_baseline.json").read_text())["per_example"][0]
            self.assertFalse(e["correct"])
            self.assertEqual(e["sql_status"], "sql_error")
            self.assertEqual(e["initial_sql_status"], "sql_error")


@unittest.skipUnless(spider_ok(), "needs Spider (scripts/get_spider.sh)")
class CleanRunStillWorks(unittest.TestCase):
    def test_clean_stub_run_writes_files_and_cost_matches(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            r = run(["score_extra_heldout.py", "5", "results/candidates.json", "baseline"],
                    {**OK_ENV, "T2S_RESULTS_DIR": str(out)}, Path(d) / "calls.log")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = json.loads((out / "heldout_baseline.json").read_text())
            self.assertAlmostEqual(res["cost_total"], 200 * 0.00055, places=6)


if __name__ == "__main__":
    unittest.main()
