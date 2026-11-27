"""Rebuild data/search.jsonl, data/heldout.jsonl and data/fewshot_pool.jsonl from Spider
by the example ids frozen in data/question_ids.json. No sampling happens here."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import os
SPIDER = Path(os.environ.get("SPIDER_DIR", ROOT / "spider_data"))

dev = json.loads((SPIDER / "dev.json").read_text())
ids = json.loads((ROOT / "data" / "question_ids.json").read_text())
(ROOT / "data").mkdir(exist_ok=True)
for name, key in (("search", "search_ids"), ("heldout", "heldout_ids")):
    with (ROOT / "data" / f"{name}.jsonl").open("w") as f:
        for eid in ids[key]:
            i = int(eid.rsplit("-", 1)[1])
            ex = dev[i]
            f.write(json.dumps({"input": ex["question"], "output": ex["query"],
                                "db_id": ex["db_id"], "example_id": eid}) + "\n")
train = json.loads((SPIDER / "train_spider.json").read_text())
with (ROOT / "data" / "fewshot_pool.jsonl").open("w") as f:
    for j, ex in enumerate(train):
        f.write(json.dumps({"id": j, "db_id": ex["db_id"], "question": ex["question"],
                            "query": ex["query"]}) + "\n")
print("wrote search.jsonl, heldout.jsonl, fewshot_pool.jsonl")
