"""Stub provider for tests: no network. Logs every completion call to $STUB_LOG.
STUB_MODE: ok (default) reply with token usage | err400 HTTP-400-like error | bad_sql reply that does not
execute (triggers repair) | nousage reply without token usage | timeout_once first call times out, then ok | timeout_always every call times out | zero_prompt reply reports 0 prompt tokens."""
import json, os, types


class Timeout(Exception):
    pass


class APIConnectionError(Exception):
    pass


model_cost = {}


def completion(**kw):
    log = os.environ.get("STUB_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"model": kw.get("model")}) + "\n")
    mode = os.environ.get("STUB_MODE", "ok")
    n = sum(1 for _ in open(log)) if log and os.path.exists(log) else 1   # this call included
    if mode == "timeout_always":
        raise Timeout("stub timeout")
    if mode == "timeout_once" and n == 1:
        raise Timeout("stub timeout")
    if mode == "err400":
        e = Exception("stub provider error")
        e.status_code = 400
        raise e
    msg = types.SimpleNamespace(content="SELECT nope FROM nowhere" if mode == "bad_sql" else "SELECT 1")
    usage = None if mode == "nousage" else types.SimpleNamespace(prompt_tokens=0 if mode == "zero_prompt" else 1000, completion_tokens=100)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
