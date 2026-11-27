# Pre-registration (written after search finished, before any held-out question was scored)

Written 2026-11-27 from search-set results only (results/trials.csv, 25 trials, 17 completed, n=100 each).
Verified: results/example_rows.jsonl contains 0 rows for any held-out example id at this moment.
The optimizer process was stopped (SIGSTOP, then killed) right after it wrote the search summary,
before its built-in held-out step scored anything. Search spend: $3.7465 (list price).

## 1. Configs scored on the 200 held-out questions (chosen from search results only)
- baseline: flash-lite, k=0, direct, full_ddl_fk, repair off, sql_only (search acc 0.88, $0.000191/q).
- search-best (harness rule: accuracy desc, then cost asc over completed trials) = the baseline config
  itself (first trial, 0.88, cheapest of the 0.88 ties). It is degenerate, so it adds nothing to score.
- best-other: best non-baseline trial by the same rule: trial_ca73b246 = gemini-3.5-flash, T=0.4, k=4, similar,
  direct, full_ddl_fk, repair on, sql_only (0.88, $0.001131/q).
- cheap-strong: cheapest trial with accuracy >= 0.87 other than the above: trial_02ba0241 = flash-lite,
  T=0.4, k=2, fixed, direct, full_ddl_fk, repair on, sql_only (0.87, $0.000254/q).
- next-strongest: trial_d56b6049 = gemini-3.5-flash, T=0.2, k=0, fixed, plan_then_sql, full_ddl_fk,
  repair on, allow_prose (0.87, $0.001590/q).
Total four configs. No other config is scored. Order: baseline first, then the rest; stop if spend nears $9.

## 2. Primary metric and comparison
Execution accuracy (result-set match, agent/sqlutil.py) on 200 questions. Each config vs baseline, paired by
question: accuracy difference with 95% CI (paired, Newcombe/Wilson-type or bootstrap; method stated in RESULTS),
and exact two-sided McNemar p on discordant pairs. Also Wilson 95% CI per config. No multiplicity correction
is applied; 3 comparisons, so p<0.05 is read cautiously (Bonferroni threshold 0.017 also reported).

## 3. Decision rule
Among scored configs, "best" = highest held-out accuracy. Ship-candidate = the cheapest config whose paired
accuracy difference vs the best is not significantly negative (McNemar p >= 0.05); if no config differs
significantly from baseline, the ship-candidate is the cheapest config (likely the baseline). A gain over
baseline is claimed only if the paired difference CI excludes 0 and McNemar p < 0.05.

## 4. Cost basis
List prices in agent/model_pricing.json (Vertex standard, global endpoint, no cache discount), applied to
provider-reported token usage. Not billed amounts.
