# Shipped with Evidence: evidence record

**Status: issued (self-issued) · protocol v1 · n=200 held-out · issued 2026-11-27 · expires 2027-02-27**

Accepted by a named person on 2026-11-27 (acceptance block at the end).

Earlier drafts called this record a certificate; it was renamed on 2026-11-27. Content unchanged.

Subject: `text-to-sql-agent` (Spider dev, SQLite, execution-match accuracy).
Machine-readable copy: [evidence.json](evidence.json). Check the hashes with
`python scripts/verify_evidence.py`.

This evidence record is issued by the people who ran the evaluation. Nobody outside checked it.
It says the protocol was followed and shows the evidence so you can check that yourself.

## What this record attests

1. **The protocol was followed.** The configurations to score, the split, the metric and the decision
   rule were written down in [PREREGISTRATION.md](PREREGISTRATION.md) before any held-out question
   was scored (hashes below).
2. **A paired held-out comparison.** Four configurations (the baseline and three alternatives) were scored on the same 200 fresh questions
   and each alternative was compared with the baseline question by question: difference with a Newcombe (method 10)
   95% interval, an exact McNemar test, and the Bonferroni threshold 0.05/3 = 0.0167. Where the interval includes 0 the finding is "no detectable gain".
3. **Archived outputs.** Per-question correctness for every scored configuration, the search trials and the
   per-example search rows are in `results/`, with hashes in `evidence.json`.
4. **Human acceptance** (block at the end). The record is not complete until a named person fills it in.

## What this record does NOT attest

- Agent quality on your data, your schema or your questions.
- The behaviour of any model after the listed model ids change.
- That Spider accuracy predicts accuracy in production.
- Provider billing. Costs are list prices applied to token counts, not invoices.

## Result recorded

No statistically detectable gain among the three held-out alternatives (two scored 174/200 against the baseline's 172/200). The best paired difference was +1.0 pp (Newcombe 95% CI -1.9 to +4.0,
McNemar p=0.69). No comparison passes the Bonferroni threshold of 0.0167 (smallest p is 0.66). By the pre-registered rule the ship choice is the baseline: `gemini-3.5-flash-lite`,
no examples, direct generation, at $0.00019 per question.

## Models covered

`vertex_ai/gemini-3.5-flash-lite`, `vertex_ai/gemini-3.5-flash`, `vertex_ai/gemini-3.1-pro-preview`
(search only, not scored on held-out). Search ran with traigent 0.33.0.dev0.

## Pre-registration hashes

Recorded 2026-11-27T13:46:08Z. Contents of `PREREG.sha256`, the original file:

```
2026-11-27T13:46:08Z
9a79d6d62580fa350abbd55247a03df0a25aef9df55f22e1953ae61b39d916c0  PREREGISTRATION.md
dc5481988bbd4541b6490a87e1fdedde9cb21dfd8333ab541872e4b1403e62c9  data/heldout.jsonl
a37e25298da04f77600a55f6156e30a1699fda5f37d1cd745b1e4254a36a2ebf  data/manifest.json
e93c4ead401a79c809f8ae384b4883a25d7b0e03f34acbc538d8aa4db6fd1d17  spec/text2sql-spider.tvl.yml
064e8223b46c525c4ac9ff577918d113ae1fa3cc4ef0fd977795a016b594cb2f  results/trials.csv
```

Pre-registration timestamps for this first run are local file times; later runs pre-register by public commit.

Two listed files were edited before publication, so their published hashes differ from the list above:
`spec/text2sql-spider.tvl.yml` (one comment line: a cloud project name removed) and `data/manifest.json`
(the local path of the source file replaced by a relative one). `data/heldout.jsonl` is not committed
(it is Spider data); `scripts/get_spider.sh` rebuilds it from the committed ids and checks it against the
original hash `dc548198...`. `PREREGISTRATION.md` and `results/trials.csv` are byte-identical to the originals.
Hashes of the published files are in `evidence.json`.

Further disclosures:

- The optimizer's own promotion gate withheld a winner, so the "search-best" configuration is the best search trial, not a gate-promoted one. It was the baseline itself.
- **Registration timing is self-attested.** The hash file and its timestamp were produced by the people who ran the evaluation; no third party witnessed them.
- **Deviation, corrected.** The first draft of the results reported Wald intervals for the paired differences. The pre-registration (section 2) named a Newcombe/Wilson-type interval or bootstrap and the 0.017 Bonferroni threshold. `analyze.py` now implements Newcombe method 10 and reports the threshold; `results/heldout_summary.json` was regenerated from the same archived per-question outputs and its hash updated. The per-question outputs were not touched. The earlier Wald intervals were narrower (best alternative -1.4 to +3.4; Newcombe -1.9 to +4.0); the verdict does not change.
- Accuracy is this repository's own SQLite execution-match scorer on a filtered subset of Spider dev, not Spider's official test-suite accuracy.
- Flash-lite temperatures in the configs are the requested values; Google documents that this model ignores a custom temperature.

## Validity

Issued 2026-11-27. Valid until 2027-02-27, or earlier if any listed model id is deprecated or retired.
After that the record is marked stale and kept, not deleted.

## Acceptance

```
Accepted by: Nimrod Busany (decision: ship the baseline per the pre-registered rule)
Accepted on: 2026-11-27
Statement:   I have read the held-out results and accept this evaluation as run.
```
