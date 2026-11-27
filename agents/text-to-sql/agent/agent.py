"""Text-to-SQL agent for Spider, tuned by Traigent from a TVL spec.

Every knob declared in spec/text2sql-spider.tvl.yml is READ at the call site in
`generate_sql` via `traigent.get_config()`:

  model              LiteLLM id (vertex_ai/<gemini model>, Vertex AI, Application
                     Default Credentials); priced from model_pricing.json
  temperature        sampling temperature
  fewshot_k          0 | 2 | 4  (enum[int] in the spec; int()'d here, also accepts "0"|"2"|"4")
  fewshot_selector   fixed   - the same k worked examples for every question
                     similar - the k Spider-train questions most similar to this one
  generation_path    direct        - emit SQL
                     plan_then_sql - short plan first, then SQL
  schema_context     none | full_ddl_fk | linked_topk (tables overlapping the question)
  repair             off | on  (on: if the SQL does not execute, re-prompt once with the error)
  output_mode        sql_only | allow_prose  (may the reply contain prose around the SQL)

Dropped from the original agent: candidate_count (self-consistency) and the
three-way repair_policy / divide_conquer path - not part of this spec.
Per-row cost (USD), latency (ms) and token counts are tracked in `CALL` so the
evaluator can emit real metrics. An unpriced model marks the row unpriced
(unknown cost, never 0).
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

import paid_guard
from sqlutil import OK, db_path, run_sql

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"

_PRICES = {k: v for k, v in json.loads((HERE / "model_pricing.json").read_text()).items()
           if not k.startswith("_")}

# Vertex AI target: Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS or
# `gcloud auth application-default login`), no API key. Set VERTEXAI_PROJECT (or
# GOOGLE_CLOUD_PROJECT) to your GCP project; VERTEXAI_LOCATION defaults to "global", the
# endpoint the list prices in model_pricing.json apply to (non-global regions carry a surcharge).
VERTEX_PROJECT = os.environ.get("VERTEXAI_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
VERTEX_LOCATION = os.environ.get("VERTEXAI_LOCATION", "global")

_local = threading.local()


class CallStats:
    """Per-row accumulator (thread-local): reset by the evaluator before each row."""

    def reset(self) -> None:
        _local.s = {"cost": 0.0, "latency_ms": 0.0, "calls": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "unpriced": False, "attempts": 0,
                    "unknown_attempts": 0, "last_error": None,
                    "initial_status": None}

    def get(self) -> dict:
        if not hasattr(_local, "s"):
            self.reset()
        return _local.s


CALL = CallStats()


def unpriced_models(models: list[str]) -> list[str]:
    """Models without a positive per-token price in model_pricing.json (unknown cost)."""
    out = []
    for m in models:
        p = _PRICES.get(m) or {}
        if not (p.get("input_cost_per_token", 0) > 0 and p.get("output_cost_per_token", 0) > 0):
            out.append(m)
    return out


# --------------------------------------------------------------------------- schema
_SCHEMA_CACHE: dict[str, str] = {}


def schema_ddl(db_id: str) -> str:
    if db_id not in _SCHEMA_CACHE:
        con = sqlite3.connect(f"file:{db_path(db_id)}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL").fetchall()
        finally:
            con.close()
        _SCHEMA_CACHE[db_id] = "\n\n".join(r[0] for r in rows)
    return _SCHEMA_CACHE[db_id]


def link_schema(schema: str, question: str, top_k_tables: int = 4) -> str:
    """Keep the CREATE TABLE blocks whose names overlap the question (cap top_k_tables)."""
    blocks = [b.strip() for b in re.split(r"(?i)(?=create\s+table)", schema) if b.strip()]
    if not blocks:
        return schema
    q = {t for t in re.findall(r"[a-z_]+", question.lower()) if len(t) > 2}

    def score(b: str) -> int:
        return len(set(re.findall(r"[a-z_]+", b.lower())) & q)

    ranked = sorted(blocks, key=score, reverse=True)
    kept = [b for b in ranked if score(b) > 0][:top_k_tables] or ranked[:top_k_tables]
    return "\n\n".join(kept)


# --------------------------------------------------------------------------- few-shot
_POOL: list[dict] | None = None
_POOL_TOK: list[set] = []
_IDF: dict[str, float] = {}
_IDF_UNSEEN = 9.0  # idf for a question word absent from the pool (about log(7001)+1)
FIXED_SEED = 7  # the "fixed" exemplars are a seeded draw from the train pool


def _tok(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) > 2}


def _load_pool() -> None:
    global _POOL
    if _POOL is not None:
        return
    _POOL = [json.loads(line) for line in (DATA / "fewshot_pool.jsonl").read_text().splitlines()]
    df: Counter = Counter()
    for ex in _POOL:
        toks = _tok(ex["question"])
        _POOL_TOK.append(toks)
        df.update(toks)
    n = len(_POOL)
    for t, c in df.items():
        _IDF[t] = math.log((n + 1) / (c + 1)) + 1.0


def pick_shots(question: str, k: int, selector: str) -> list[dict]:
    if k <= 0:
        return []
    _load_pool()
    assert _POOL is not None
    if selector == "fixed":
        rng = random.Random(FIXED_SEED)
        return [_POOL[i] for i in rng.sample(range(len(_POOL)), k=4)][:k]
    qt = _tok(question)
    scored = []
    for i, toks in enumerate(_POOL_TOK):
        inter = qt & toks
        if inter:
            scored.append((sum(_IDF[t] for t in inter) / (sum(_IDF.get(t, _IDF_UNSEEN) for t in qt | toks) or 1), -i, i))
    scored.sort(reverse=True)
    return [_POOL[i] for _, _, i in scored[:k]]


# --------------------------------------------------------------------------- prompt
_BASE = ("You are an expert SQLite analyst. Translate the question into ONE valid "
         "SQLite SELECT query. Use only tables/columns that exist in the schema. "
         "Return exactly the columns the question asks for, and no others.")


def build_messages(question, db_id, *, schema_context, fewshot_k, fewshot_selector,
                   generation_path, output_mode):
    if generation_path == "plan_then_sql":
        fmt = ("First write a brief plan (tables, joins, filters, aggregation) in 1-3 lines. "
               "Then give the final query in a ```sql fenced block. Nothing after the block.")
    elif output_mode == "allow_prose":
        fmt = ("You may explain briefly, but give the final query in a ```sql fenced block. "
               "Nothing after the block.")
    else:
        fmt = "Return ONLY the SQL query: no prose, no markdown, no code fences."
    system = f"{_BASE}\n{fmt}"
    if schema_context != "none":
        ddl = schema_ddl(db_id)
        shown = ddl if schema_context == "full_ddl_fk" else link_schema(ddl, question)
        system += f"\n\nDatabase schema:\n{shown}"
    shots = pick_shots(question, fewshot_k, fewshot_selector)
    if shots:
        system += "\n\nExamples (other databases):\n" + "\n".join(
            f"Question: {s['question']}\nSQL: {s['query']}" for s in shots)
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}"}]


# --------------------------------------------------------------------------- completion
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    fences = _FENCE.findall(text)
    if fences:
        text = fences[-1]
    elif re.search(r"(?im)^\s*SQL\s*:", text):
        text = re.split(r"(?im)^\s*SQL\s*:", text)[-1]
    kw = re.search(r"\b(SELECT|WITH)\b", text, re.IGNORECASE)
    if kw:
        text = text[kw.start():]
    # first complete statement (respects ';' inside string literals)
    for m in re.finditer(";", text):
        if sqlite3.complete_statement(text[: m.end()]):
            return text[: m.start()].strip()
    return text.strip().strip("`").strip()


class ProviderCallFailed(RuntimeError):
    """The provider call failed after the bounded retry (no completion was returned)."""


MAX_ATTEMPTS = 2        # one call + ONE bounded retry on a transient provider error
RETRY_BACKOFF_S = 2.0


def _classify_failure(exc: BaseException) -> tuple[bool, bool]:
    """-> (transient, billed_unknown) for a failed provider call.

    A provider that ANSWERED with an HTTP error (429/5xx/4xx, status_code on the exception)
    returned no completion and did not bill: known $0, 0 tokens (billed_unknown=False).
    A client-side timeout or a dropped connection is ambiguous: the request may have been
    processed and billed, so its cost is UNKNOWN (billed_unknown=True) and is never
    reported as 0.
    """
    import litellm
    status = getattr(exc, "status_code", None)
    ambiguous = isinstance(exc, (litellm.Timeout, litellm.APIConnectionError)) or \
        not isinstance(status, int)
    transient = ambiguous or status == 429 or (isinstance(status, int) and status >= 500)
    return transient, ambiguous


def complete(model: str, messages: list[dict], temperature: float, max_tokens: int) -> str:
    """One LiteLLM call (+ one bounded retry on transient errors) -> raw reply text.

    Accumulates real cost/latency/tokens of EVERY attempt in CALL. Raises ProviderCallFailed
    when no completion was obtained; CALL then records whether any failed attempt may have
    been billed (`unknown_attempts`) so the evaluator can report cost truthfully.
    """
    import litellm  # imported lazily; the runner imports `traigent` first (price-map order)

    st = CALL.get()
    resp = None
    real = bool(paid_guard.SPEND["real"])
    max_attempts = 1 if real else MAX_ATTEMPTS      # no retries in a paid run
    for attempt in range(1, max_attempts + 1):
        paid_guard.before_provider_call()   # refuses unless the run was armed and under its cap
        t0 = time.time()
        st["attempts"] += 1
        try:
            resp = litellm.completion(model=model, messages=messages, temperature=temperature,
                                      max_tokens=max_tokens + int(_PRICES.get(model, {}).get("max_tokens_headroom", 0)),
                                      vertex_project=VERTEX_PROJECT, vertex_location=VERTEX_LOCATION,
                                      allowed_openai_params=["temperature"],  # litellm lacks flash-lite in its param table; Vertex accepts it
                                      **({"num_retries": 0} if real else {}),
                                      reasoning_effort=_PRICES.get(model, {}).get("reasoning_effort", "minimal"))
            st["latency_ms"] += (time.time() - t0) * 1000.0
            break
        except Exception as exc:  # provider failure: classify, maybe retry once
            st["latency_ms"] += (time.time() - t0) * 1000.0
            transient, unknown = _classify_failure(exc)
            if unknown:
                st["unknown_attempts"] += 1
            st["last_error"] = type(exc).__name__
            if real:   # global rule: any provider exception in a paid run aborts everything
                paid_guard.abort(f"provider exception {type(exc).__name__}")
                raise ProviderCallFailed(f"{type(exc).__name__}; paid run aborted") from exc
            if not transient or attempt == max_attempts:
                raise ProviderCallFailed(f"{type(exc).__name__} after {attempt} attempt(s)") from exc
            time.sleep(RETRY_BACKOFF_S)
    assert resp is not None
    st["calls"] += 1
    usage = getattr(resp, "usage", None)
    pt = getattr(usage, "prompt_tokens", None)
    ct = getattr(usage, "completion_tokens", None)
    price = _PRICES.get(model) or {}
    pin, pout = price.get("input_cost_per_token"), price.get("output_cost_per_token")
    if paid_guard.usage_unknown(pt, ct) or not (pin and pout and pin > 0 and pout > 0):
        st["unpriced"] = True  # unknown cost - never silently 0
        if paid_guard.SPEND["real"]:
            paid_guard.abort("a response could not be priced")   # no further provider calls
            raise paid_guard.Refused("provider reply could not be priced (missing usage or price); run aborted")
    else:
        st["prompt_tokens"] += int(pt)
        st["completion_tokens"] += int(ct)
        delta = pt * pin + ct * pout
        st["cost"] += delta
        paid_guard.record_cost(delta)   # per call, so the cap is checked before the next call (repair included)
    return str(resp.choices[0].message.content or "")


def vertex_preflight(models: list[str]) -> list[str]:
    """Auth + one tiny real call per model (a few tokens each). -> list of problems ([] = ok)."""
    problems = []
    for m in models:
        try:
            r = litellm_call(m)
            if getattr(getattr(r, "usage", None), "completion_tokens", None) is None:
                problems.append(f"{m}: reply carried no token usage (cost could not be measured)")
            else:
                print(f"[preflight] {m}: ok ({r.usage.prompt_tokens} in / {r.usage.completion_tokens} out tokens)",
                      flush=True)
        except Exception as exc:
            problems.append(f"{m}: {type(exc).__name__} (HTTP {getattr(exc, 'status_code', '?')}): {str(exc)[:200]}")
    return problems


def litellm_call(model: str):
    import litellm
    paid_guard.before_provider_call()
    try:
        resp = litellm.completion(
            model=model, messages=[{"role": "user", "content": "Reply with OK"}], temperature=0.0,
            max_tokens=8 + int(_PRICES.get(model, {}).get("max_tokens_headroom", 0)),
            vertex_project=VERTEX_PROJECT, vertex_location=VERTEX_LOCATION,
            allowed_openai_params=["temperature"],
            **({"num_retries": 0} if paid_guard.SPEND["real"] else {}),
            reasoning_effort=_PRICES.get(model, {}).get("reasoning_effort", "minimal"))
    except Exception as exc:
        if paid_guard.SPEND["real"]:    # global rule: any provider exception in a paid run aborts everything
            paid_guard.abort(f"provider exception {type(exc).__name__}")
        raise
    p = _PRICES.get(model) or {}
    u = getattr(resp, "usage", None)
    pt, ct = getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None)
    pin, pout = p.get("input_cost_per_token"), p.get("output_cost_per_token")
    if paid_guard.usage_unknown(pt, ct) or not (pin and pout and pin > 0 and pout > 0):
        if paid_guard.SPEND["real"]:
            paid_guard.abort("a preflight reply could not be priced")   # no further provider calls
            raise paid_guard.Refused("preflight reply could not be priced (missing usage or price); run aborted")
    else:
        paid_guard.record_cost(pt * pin + ct * pout)
    return resp


def generate_sql(question: str, db_id: str) -> str:
    """Translate one question into SQL under the current Traigent config."""
    import traigent

    cfg = traigent.get_config()
    model = str(cfg["model"])
    temperature = float(cfg.get("temperature", 0.0))
    fewshot_k = int(cfg.get("fewshot_k", 0))
    fewshot_selector = str(cfg.get("fewshot_selector", "fixed"))
    generation_path = str(cfg.get("generation_path", "direct"))
    schema_context = str(cfg.get("schema_context", "full_ddl_fk"))
    repair = str(cfg.get("repair", "off"))
    output_mode = str(cfg.get("output_mode", "sql_only"))

    messages = build_messages(
        question, db_id, schema_context=schema_context, fewshot_k=fewshot_k,
        fewshot_selector=fewshot_selector, generation_path=generation_path,
        output_mode=output_mode)
    max_tokens = 700 if (generation_path == "plan_then_sql" or output_mode == "allow_prose") else 400
    raw = complete(model, messages, temperature, max_tokens)
    sql = extract_sql(raw)

    if repair == "on":
        status, _ = run_sql(sql, db_id)
        CALL.get()["initial_status"] = status   # SQLite outcome of the first query, before any repair
        if status != OK:
            why = "timed out" if status == "timeout" else "failed to execute"
            fix_msgs = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": (
                    f"That query {why} on SQLite. Return a corrected single SELECT. "
                    "Same output format as before.")}]
            sql = extract_sql(complete(model, fix_msgs, 0.0, max_tokens))
    return sql
