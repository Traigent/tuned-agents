"""Check that the files in this repo still match the hashes recorded in evidence.json.
usage: python scripts/verify_evidence.py   (run scripts/get_spider.sh first so data/heldout.jsonl exists)"""
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ev = json.loads((ROOT / "evidence.json").read_text())
bad = 0
for group in ("preregistered_inputs", "archived_outputs"):
    for rel, want in ev[group].items():
        p = ROOT / rel
        got = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "MISSING"
        ok = got == want
        bad += not ok
        print(("ok      " if ok else "MISMATCH"), rel)
sys.exit(1 if bad else 0)
