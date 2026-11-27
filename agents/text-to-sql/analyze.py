"""Paired held-out analysis, with the methods named in PREREGISTRATION.md section 2.

usage: python analyze.py <baseline_label> <other_label> ...   (first label is the baseline)
Reads <T2S_RESULTS_DIR or results>/heldout_<label>.json, prints the scorecard, writes heldout_summary.json.

Per config: Wilson 95% CI on accuracy. Against the baseline, paired by question: difference with a
Newcombe (1998, method 10) 95% interval built from Wilson intervals and the paired correlation, and the
exact two-sided McNemar p on the discordant pairs. Bonferroni threshold = 0.05 / number of comparisons.
"""
import json, math, os, sys
from math import comb

OUT = os.environ.get("T2S_RESULTS_DIR", "results")
Z = 1.96


def wilson(k, n, z=Z):
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def mcnemar(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def newcombe_paired(a, b, c, d, z=Z):
    """Newcombe method 10: CI for p1 - p2 from a paired 2x2 table.
    a = both correct, b = first (config) only, c = second (baseline) only, d = neither."""
    n = a + b + c + d
    p1, p2 = (a + b) / n, (a + c) / n
    l1, u1 = wilson(a + b, n, z); l2, u2 = wilson(a + c, n, z)
    A = (a + b) * (c + d) * (a + c) * (b + d)
    C = a * d - b * c
    D = C - n / 2 if C > n / 2 else (0.0 if C >= 0 else C)
    phi = D / math.sqrt(A) if A > 0 else 0.0
    lo = (p1 - p2) - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2))
    hi = (p1 - p2) + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2))
    return lo, hi


def main(labels):
    H = {l: json.load(open(f"{OUT}/heldout_{l}.json")) for l in labels}
    P = {l: {e["example_id"]: e["acc"] == 1.0 for e in H[l]["per_example"]} for l in labels}
    base = labels[0]
    ids = sorted(P[base])
    n = len(ids)
    bonf = 0.05 / max(1, len(labels) - 1)
    out = {}
    for l in labels:
        k = sum(P[l][i] for i in ids)
        lo, hi = wilson(k, n)
        rec = dict(correct=k, n=n, acc=k / n, ci=[round(lo, 4), round(hi, 4)],
                   cost_total=H[l]["cost_total"], cost_per_q=H[l]["cost_per_query"],
                   latency_ms_mean=H[l]["latency_ms_mean"], errors=H[l]["errors"], config=H[l]["config"])
        if l != base:
            a = sum(P[l][i] and P[base][i] for i in ids); b = sum(P[l][i] and not P[base][i] for i in ids)
            c = sum(P[base][i] and not P[l][i] for i in ids); d = n - a - b - c
            dlo, dhi = newcombe_paired(a, b, c, d)
            p = mcnemar(b, c)
            rec.update(wins_vs_base=b, losses_vs_base=c, diff_vs_base=round((b - c) / n, 6),
                       diff_ci_newcombe=[round(dlo, 6), round(dhi, 6)], mcnemar_p=p,
                       bonferroni_threshold=round(bonf, 4), passes_bonferroni=p < bonf)
        out[l] = rec
    json.dump(out, open(f"{OUT}/heldout_summary.json", "w"), indent=2)
    print(f"{'config':16} {'acc (Wilson 95% CI)':26} {'wins/losses':>11} {'diff vs base (Newcombe 95% CI)':>34} {'McNemar p':>9} {'$/q':>8} {'ms':>6}")
    for l, v in out.items():
        acc = f"{v['correct']}/{n} = {100*v['acc']:.1f}% ({100*v['ci'][0]:.1f}-{100*v['ci'][1]:.1f})"
        if l == base:
            print(f"{l:16} {acc:26} {'-':>11} {'-':>34} {'-':>9} {v['cost_per_q']:8.5f} {v['latency_ms_mean']:6.0f}")
        else:
            dd = f"{100*v['diff_vs_base']:+.1f} pp ({100*v['diff_ci_newcombe'][0]:+.1f} to {100*v['diff_ci_newcombe'][1]:+.1f})"
            print(f"{l:16} {acc:26} {str(v['wins_vs_base'])+'/'+str(v['losses_vs_base']):>11} {dd:>34} {v['mcnemar_p']:9.2f} {v['cost_per_q']:8.5f} {v['latency_ms_mean']:6.0f}")
    print(f"Bonferroni threshold for {len(labels)-1} comparisons: p < {bonf:.4f}; "
          f"comparisons passing it: {sum(v.get('passes_bonferroni', False) for v in out.values())}")


if __name__ == "__main__":
    main(sys.argv[1:])
