"""SQLite execution + Spider execution-match scoring.

Scoring rules:

- Candidate SQL runs on a READ-ONLY handle (URI mode=ro) with a default-deny
  authorizer, a wall-clock watchdog (not a VM-step cap), and size caps.
- Rows compare as a multiset unless the gold query has ORDER BY (then order
  matters). Column ORDER is forgiven (every column permutation is tried);
  column COUNT must match. An empty gold never scores (excluded upstream).
- A timeout is reported separately from an SQL error; both score 0.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from collections import Counter
from itertools import permutations
from pathlib import Path
from typing import Any

# scripts/get_spider.sh unpacks Spider into <repo>/spider_data; override with SPIDER_DIR.
SPIDER_DIR = Path(os.environ.get("SPIDER_DIR", str(Path(__file__).resolve().parent.parent / "spider_data")))
QUERY_BUDGET_S = float(os.environ.get("SQL_QUERY_BUDGET_S", "5.0"))  # wall clock per statement

OK, ERROR, TIMEOUT = "ok", "error", "timeout"


def db_path(db_id: str) -> Path:
    return SPIDER_DIR / "database" / db_id / f"{db_id}.sqlite"


_READ_ONLY_FUNCTIONS = frozenset({
    "abs", "avg", "char", "coalesce", "concat", "concat_ws", "count",
    "date", "datetime", "format", "glob", "group_concat", "hex", "ifnull",
    "iif", "instr", "json", "json_array", "json_array_length", "json_extract",
    "json_group_array", "json_group_object", "json_insert", "json_object",
    "json_patch", "json_quote", "json_remove", "json_replace", "json_set",
    "json_type", "json_valid", "julianday", "length", "like", "likelihood",
    "likely", "lower", "ltrim", "max", "min", "nullif", "printf", "quote",
    "random", "replace", "round", "rtrim", "sign", "strftime",
    "substr", "substring", "sum", "time", "total", "trim", "typeof",
    "unicode", "unixepoch", "unlikely", "upper",
    "->", "->>",
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
    "lag", "lead", "first_value", "last_value", "nth_value",
    "acos", "acosh", "asin", "asinh", "atan", "atan2", "atanh", "ceil",
    "ceiling", "cos", "cosh", "degrees", "exp", "floor", "ln", "log", "log10",
    "log2", "mod", "pi", "pow", "power", "radians", "sin", "sinh", "sqrt",
    "tan", "tanh", "trunc",
})


def _authorizer(action, arg1, arg2, database_name, trigger_name):
    # Default-deny: only table reads, SELECT control flow and data-only built-ins.
    if action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and isinstance(arg2, str):
        if arg2.lower() in _READ_ONLY_FUNCTIONS:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run_sql(sql: str, db_id: str) -> tuple[str, list[tuple[Any, ...]] | None]:
    """Run `sql` read-only. Returns (status, rows) with status ok|error|timeout."""
    path = db_path(db_id)
    if not path.exists():
        return ERROR, None
    con = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.text_factory = lambda b: b.decode("utf-8", errors="replace")
        con.set_authorizer(_authorizer)
        deadline = time.monotonic() + QUERY_BUDGET_S
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        rows: list[tuple[Any, ...]] = []
        size = 0
        for row in con.execute(sql):
            size += sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in row)
            if size > 50_000_000:
                return ERROR, None
            rows.append(row)
        return OK, rows
    except sqlite3.OperationalError as exc:
        return (TIMEOUT if str(exc) == "interrupted" else ERROR), None
    except Exception:
        return ERROR, None
    finally:
        if con is not None:
            con.close()


def _norm_cell(cell: Any) -> Any:
    if isinstance(cell, float):
        return round(cell, 4)
    return cell


def _norm(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [tuple(_norm_cell(c) for c in r) for r in rows]


def result_eq(pred_rows, gold_rows, gold_has_order_by: bool) -> bool:
    """Spider convention: rows = multiset unless gold has ORDER BY; column order
    forgiven (permutations); column count must match; empty gold never matches."""
    pred_rows, gold_rows = _norm(pred_rows), _norm(gold_rows)
    if len(pred_rows) != len(gold_rows):
        return False
    if not gold_rows:
        return False
    n = len(gold_rows[0])
    if len(pred_rows[0]) != n:
        return False
    for perm in permutations(range(n)):
        p = [tuple(r[i] for i in perm) for r in pred_rows]
        if (p == gold_rows) if gold_has_order_by else (Counter(p) == Counter(gold_rows)):
            return True
    return False


_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


def exec_match(pred_sql: str, gold_sql: str, db_id: str) -> tuple[float, str]:
    """(score, status) where status is ok | error | timeout | gold_error."""
    g_status, g_rows = run_sql(gold_sql, db_id)
    if g_status != OK or not g_rows:
        return 0.0, "gold_error"
    p_status, p_rows = run_sql(pred_sql, db_id)
    if p_status != OK or p_rows is None:
        return 0.0, p_status
    return (1.0 if result_eq(p_rows, g_rows, bool(_ORDER_BY.search(gold_sql))) else 0.0), OK
