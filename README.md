# Tuned Agents

Useful AI agents, tuned with Traigent and every agent ships with its evidence: scorecard, TVL spec, failures, and a one-command rerun.

## What the evidence record means

Each agent is Shipped with Evidence: an evidence record. It is self-issued. It attests two things: the pre-registered evaluation protocol was followed (hashes of the pre-registration, data splits and archived outputs are checked by a script), and a named human accepted the result. It does not attest quality on your data, and it expires (each evidence record states its date).

## Agents

| Agent | Task | Verdict | Evidence | Link |
|-------|------|---------|-------------------|------|
| text-to-sql | Natural-language question to SQLite query, scored by execution match on Spider dev | Cheapest baseline kept: no statistically detectable gain from 3 tuned alternatives on 200 held-out Spider questions; others cost up to ~8x more | [![Shipped with Evidence](https://img.shields.io/badge/Shipped%20with%20Evidence-n%3D200%20held--out%20%C2%B7%202026--11%20%C2%B7%20exp%202027--02-blue)](agents/text-to-sql/EVIDENCE.md) | [agents/text-to-sql](agents/text-to-sql) |

Every agent lives in its own folder under `agents/` and runs from there: `cd agents/text-to-sql`, then follow its README.

## How agents are added

An agent is listed only if it meets all of these:

- A written pre-registration (hypotheses, configuration space, decision rule, metric) fixed before the held-out run, with file hashes.
- A paired comparison on held-out data against a baseline, with confidence intervals and an exact test.
- A public benchmark, so anyone can obtain the same data.
- Exact model ids, spend, archived raw outputs, and a rerun path (mock mode first, spend guarded).
- A named human who accepted the result.
- An expiry date on the evidence record.
- Real failures shown, not only the scorecard.

## Built with Traigent

Agents are tuned with [Traigent](https://github.com/Traigent/Traigent) ([traigent.ai](https://traigent.ai)).

## Licence

Code in this repository: MIT (see [LICENSE](LICENSE)). Agents depend on the Traigent SDK, which is AGPL-3.0 or under a commercial licence. Datasets keep their own licences; see each agent's NOTICE (for example [agents/text-to-sql/NOTICE](agents/text-to-sql/NOTICE)).
