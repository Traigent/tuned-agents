# text-to-sql

[![Shipped with Evidence](https://img.shields.io/badge/Shipped%20with%20Evidence-n%3D200%20held--out%20%C2%B7%202026--11%20%C2%B7%20exp%202027--02-blue)](EVIDENCE.md)

Text-to-SQL agent for SQLite (Spider), tuned with Traigent and evaluated on 200 held-out questions.

**We searched 25 proposed configurations (24 distinct) across 3 Gemini models. On 200 fresh held-out questions we found no statistically detectable gain among the three alternatives scored against the cheapest baseline (best +1.0 pp, 95% CI −1.9 to +4.0, McNemar p=0.69). The pre-registered rule ships the baseline at $0.00019 per question; the other configurations cost up to ~8x more for no measurable gain.**

## Scorecard

200 held-out Spider dev questions, execution-match accuracy from this repo's own SQLite scorer (`agent/sqlutil.py`: result sets compared, column order forgiven, row order only when the gold has ORDER BY), not Spider's official test-suite accuracy; each configuration compared with the baseline on the same questions. Accuracy intervals are Wilson 95%. Paired-difference intervals are Newcombe (method 10) 95%, the paired method named in the pre-registration; McNemar is exact two-sided. Bonferroni threshold for the 3 comparisons: p < 0.0167. No comparison passes it (smallest p is 0.66).

| Config | Model id | Held-out accuracy (Wilson 95% CI) | Diff vs baseline (95% CI) | McNemar p | $/question | Mean latency |
|---|---|---|---|---|---|---|
| baseline: k=0, direct, T=0 | `vertex_ai/gemini-3.5-flash-lite` | 86.0% (80.5 to 90.1) | n/a | n/a | $0.00019 | 746 ms |
| cheap-strong: k=2 fixed, repair on, T=0.4 | `vertex_ai/gemini-3.5-flash-lite` | 87.0% (81.6 to 91.0) | +1.0 pp (−1.9 to +4.0) | 0.69 | $0.00026 | 810 ms |
| best-other: k=4 similar, repair on, T=0.4 | `vertex_ai/gemini-3.5-flash` | 87.0% (81.6 to 91.0) | +1.0 pp (−3.0 to +5.0) | 0.79 | $0.00113 | 1183 ms |
| next-strongest: plan then SQL, repair on, T=0.2 | `vertex_ai/gemini-3.5-flash` | 84.5% (78.8 to 88.9) | −1.5 pp (−6.3 to +3.2) | 0.66 | $0.00158 | 1461 ms |

Every paired interval includes 0. Two alternatives scored 174/200 against the baseline's 172/200; that two-question gap is within noise, not a finding.

Temperatures are the values requested in the config. Google's documentation says Gemini 3.5 Flash-Lite ignores a custom temperature ([model page](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash-lite)), so for the two flash-lite configs scored on held-out the temperature setting may have had no effect. Full configs: [results/candidates.json](results/candidates.json). Raw per-question outputs: `results/heldout_*.json`.
Costs are provider-reported tokens times list prices ([agent/model_pricing.json](agent/model_pricing.json)), not billed amounts.

Evidence record: [EVIDENCE.md](EVIDENCE.md), self-issued and accepted on 2026-11-27. It attests that the protocol was followed, not that the agent is good on your data.

## How it was built

- **Data.** A filtered subset of Spider dev (gold queries that error or return no rows removed). Search set n=100, held-out set n=200, disjoint, drawn with seed 20261127 across 19 databases each. 120 questions used in an earlier trial run and gold queries that error or return no rows were excluded, so the held-out set is fresh. Few-shot examples come from Spider train (different databases from dev).
- **Search.** The configuration space is in [spec/text2sql-spider.tvl.yml](spec/text2sql-spider.tvl.yml): model (3 Gemini models via Vertex AI) × temperature {0, 0.2, 0.4} × few-shot count {0, 2, 4} × example selector {fixed, similar} × generation {direct, plan then SQL} × schema {none, full DDL with foreign keys, linked top-k tables} × repair {off, on} × output {SQL only, allow prose}, with 2 structural constraints. Traigent proposed 25 trials (24 distinct configurations): 17 ran, 8 were rejected by the constraints. Each ran on the 100 search questions.
- **Pre-registration.** After the search and before any held-out question was scored, the four configurations to score and the decision rule were written down and hashed ([PREREGISTRATION.md](PREREGISTRATION.md), [PREREG.sha256](PREREG.sha256)). Four configurations were scored: the baseline (which was also the search-best) plus 3 runners-up. The registration time is self-attested: the hash file and its timestamp were made by the same people who ran the evaluation, from local file times.
- **Held-out.** Each configuration was scored once on the 200 questions. Rule: ship the cheapest configuration that is not significantly worse than the best; claim a gain only if the paired CI excludes 0 and p < 0.05.
- **Spend.** About $4.38 in total (list-price estimate from token counts, not a billing statement): search $3.75, held-out scoring $0.63.
- **Search winner.** The SDK's own promotion gate withheld a winner (no reason was recorded in the run summary), so "search-best" is the best search trial, not a gate-promoted one.

## Failures

Real baseline failures on the held-out set (28 of 200). The archived run stored correctness per question, not the SQL, so the predictions below were regenerated with the baseline configuration (27 of the 28 failed again; one passed on re-run, so outputs are not perfectly repeatable; the temperature was requested as 0, and may be ignored by this model). Regenerate them with `scripts/rerun_baseline_failures.py`.

1. **spider-dev-0493: extra column.** Question: "List the name, date and result of each battle." Gold: `SELECT name, date FROM battle`. Predicted: `SELECT name, date, result FROM battle`. The prediction follows the question; the gold omits `result`. Scored wrong: a dataset label issue.
2. **spider-dev-0867: extra column.** Question: "What is the grade of each high schooler?" Gold: `SELECT grade FROM Highschooler`. Predicted: `SELECT name, grade FROM Highschooler`. Wrong: returns one column too many.
3. **spider-dev-0851: sort direction.** Question: "What are the major record formats of orchestras, sorted by their frequency?" Gold (shortened): `... GROUP BY Major_Record_Format ORDER BY COUNT(*) ASC`. Predicted (shortened): same with `DESC`. Wrong: the question does not say which direction; the prediction guessed the opposite of the gold.
4. **spider-dev-0439: ambiguous question.** Question: "Find the highest rank of losers in all matches." Gold: `SELECT min(loser_rank) FROM matches`. Predicted: `SELECT MAX(loser_rank) FROM matches`. In tennis rankings a smaller number is a higher rank, so the gold is defensible and the model read "highest" as the largest number.
5. **spider-dev-0180: value spelling.** Question: "What country is Jetblue Airways affiliated with?" Gold (shortened): `... WHERE Airline = "JetBlue Airways"`. Predicted (shortened): `... WHERE Airline = 'Jetblue Airways'`. Wrong: the question spells the name differently from the stored value and SQLite `=` is case-sensitive. The model copied the question; the gold uses the database value.
6. **spider-dev-0101 (from the earlier trial run, not in this held-out set): wrong table.** The baseline generated `SELECT T1.Make, T1.Year FROM cars_data AS T1 ...`. The column `Make` lives in `car_names`, so SQLite raised "no such column" and the row scored 0. A model error, not a harness bug: re-running the question reproduced the same SQL.

The 6 above are a mix of model mistakes, ambiguous questions and gold-label quirks; we did not classify all 28.

## Reproduce

You bring your own Vertex AI (Gemini) credentials. Python 3.12 was used.

```bash
cd agents/text-to-sql   # from the collection root; every command below runs from here
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 1. Get Spider and rebuild the exact splits by id (checks the held-out hash).
#    The script downloads with gdown (pip install gdown). If that fails, download spider.zip by hand
#    from https://yale-lily.github.io/spider and run:  export SPIDER_ZIP=/path/to/spider.zip
#    (or unpack it yourself and run:  export SPIDER_DIR=/path/to/spider_data)
#    Use `export` (not an inline variable): every later command below reads SPIDER_DIR from the shell.
#    If you let the script download, Spider lands in ./spider_data, which is the default.
scripts/get_spider.sh

# 2. Mock mode first: offline, no network, no spend, canned model replies
python run_optimization.py --mock
python -m unittest tests.test_paid_guard   # spend-guard tests, stub provider, no network
python scripts/verify_evidence.py          # archived files match evidence.json
python analyze.py baseline best_other cheap_strong next_strongest   # full scorecard from the archived outputs, no model call

# 3. Real run (paid Vertex AI calls; the search was about $3.75)
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json   # or: gcloud auth application-default login
export VERTEXAI_PROJECT=your-gcp-project       # or GOOGLE_CLOUD_PROJECT
export VERTEXAI_LOCATION=global                # default; list prices assume the global endpoint
export TRAIGENT_COST_APPROVED=true TRAIGENT_RUN_COST_LIMIT=10
python run_optimization.py --real              # search only, writes results-rerun/

# 4. Score the pre-registered configurations on the held-out set, then analyse
export T2S_RESULTS_DIR=results-rerun
python score_extra_heldout.py 5 results/candidates.json baseline best_other cheap_strong next_strongest
python analyze.py baseline best_other cheap_strong next_strongest
```

Step 4 as written re-scores the archived pre-registered configurations (`results/candidates.json`). For a fresh search, pick your runners-up, write your own pre-registration and candidates file first, and pass that file as the second argument. The scoring script refuses to run without `TRAIGENT_COST_APPROVED=true` and a spend limit of at most $10, and it writes nothing if any question was skipped by the cap or a provider failure.
All three paid entry points (`run_optimization.py --real`, `score_extra_heldout.py`, `scripts/rerun_baseline_failures.py`) share one guard (`agent/paid_guard.py`): they refuse unless `TRAIGENT_COST_APPROVED=true`, the cap is the smaller of the command-line limit and `TRAIGENT_RUN_COST_LIMIT` (each in (0, 10]), and spend is recorded after every provider call (repair calls and retries included), with the cap checked before each call. In a paid run any provider exception, timeout or reply of unknown cost aborts everything at once: nothing is retried, no further call is made, the command exits non-zero and prints the attempted-call count and known spend. Mock mode is unchanged. A model query that fails to execute or times out in SQLite is a wrong answer: it is scored incorrect and recorded per question (`correct`, `sql_status`, and `initial_sql_status` for the first query before any repair). Scoring stops, exits non-zero and writes nothing if any question hit a provider error, a retry or timeout, a gold_error, the cap, or an unpriced reply.
Model outputs are not perfectly repeatable, so a rerun will differ somewhat from the numbers above.
The analysis in step 2 writes `heldout_summary.json` into `results/` (or `T2S_RESULTS_DIR` if set); with the archived outputs the content is the committed file.

Status of these commands: step 1 (rebuild and hash check), step 2 (mock run, from a clean Python 3.12 venv with the pinned requirements) and the archived-output analysis were run before publication. The `get_spider.sh` download step was **not** run: it fetches Spider from a link recorded in the script and needs `gdown`, so it is unverified; the manual ZIP route above is the fallback, and the script checks the result against the hash. The real run in step 3 was originally done connected to the Traigent backend; the offline path in this repo was not re-run end to end (unverified).

## Cost safety

The spend cap and abort rules in this repo (`agent/paid_guard.py`) are best-effort, in-process checks. The hard limit has to be provider-side: before any paid run, set a Google Cloud billing budget with alerts, or a quota, on the project you use. Known residual gaps:

- Continuation requests that LiteLLM makes inside one call can be invisible to the guard, so a call can cost more than the guard records.
- The Traigent SDK's per-example timeout can leave one more provider call in flight that completes after the guard has aborted.
- Costs are list prices applied to reported token counts; a reply with missing usage or zero prompt tokens aborts the run, but an under-reported count would not be caught.

Paid mode also sets `TRAIGENT_VENDOR_MAX_RETRIES=0` so the SDK does not retry provider calls.

## Dependencies

- **Traigent SDK** (`traigent`), licensed AGPL-3.0 or under a commercial license. The search ran on `0.33.0.dev0`, a development build that is not on PyPI; `requirements.txt` pins the released `0.33.0`, the nearest release. Mock mode was run on `0.33.0`; the archived search results come from the dev build.
- LiteLLM 1.111.0 for the model calls; Google Cloud client libraries for Vertex AI auth.
- **Spider** (Yale LILY lab) is CC BY-SA 4.0: Yu et al., EMNLP 2018, https://yale-lily.github.io/spider. This repo does not contain the Spider dataset (only example ids). Quoted Spider questions and gold queries in this README (some shortened) remain CC BY-SA 4.0; see [NOTICE](NOTICE).
- This repository's own code is MIT ([LICENSE](LICENSE)).

## Limits

- **Power.** With n=200 only a gain of roughly 5 pp or more is detectable, as a rough approximation for this sample size and a baseline near 86%. "No detectable gain" is not "no gain": a true +2 pp would look like this.
- **Costs are list prices** applied to token counts, not billing.
- **One benchmark, one seed.** Spider dev may be in the models' training data. The held-out questions come from 19 databases, so they are not fully independent. No multiplicity correction was applied across the 3 comparisons.
- **Preview model ids.** `gemini-3.1-pro-preview` is a preview id and may change or be retired; the evidence record expires early if any listed id is deprecated.
- **Not your data.** This says nothing about your schema or questions; run the harness on your own questions before relying on any configuration.
- **Self-issued.** No outside party reviewed this. Pre-registration timestamps for this first run are local file times; later runs pre-register by public commit.
- **Accepted, not audited.** A named person accepted the release; no outside party audited it.
