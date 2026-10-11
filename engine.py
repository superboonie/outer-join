"""The query engine: plan (Gemini) -> scan (Nimble, parallel) -> bind (Tiger) -> execute (SQL) -> repair."""
import datetime as dt
import json
import re
import time
import uuid
from urllib.parse import quote_plus
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import sentry_sdk

import llm
import sources
import store


def _fill(obj, bind):
    """Replace {col} placeholders with values from a local row (the bind join)."""
    if isinstance(obj, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(bind.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: _fill(v, bind) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, bind) for v in obj]
    return obj


def _mock_plan(q, local):
    note = "MOCK PLAN: set GEMINI_API_KEY for real planning."
    if "my_products" in local and re.search(r"\b(my|our|undercut|catalog|sku)", q, re.I):
        return {"notes": note, "scans": [{"table": "amazon", "kind": "template", "template": "amazon_serp",
                "params": {"keyword": "{name}"}, "for_each": "SELECT name FROM my_products LIMIT 12"}]}
    return {"notes": note, "scans": [{"table": "amazon", "kind": "template", "template": "amazon_serp", "params": {"keyword": q}}]}


def _mock_sql(tables):
    name, cols = next(iter(tables.items()))
    names = {c for c, _ in cols}
    if {"_bind_name", "price"} <= names:
        return f"""SELECT p.sku, p.name, p.our_price, w.price AS web_price,
       round((p.our_price - w.price)::numeric, 2) AS we_are_higher_by, w._source AS source
FROM my_products p
JOIN LATERAL (SELECT * FROM {name} x WHERE x._bind_name = p.name AND x.price IS NOT NULL
              ORDER BY x.price LIMIT 1) w ON true
WHERE w.price < p.our_price ORDER BY we_are_higher_by DESC LIMIT 50"""
    return f"SELECT * FROM {name} LIMIT 25"


def _key(call):
    ident = {k: call.get(k) for k in ("kind", "template", "params", "query", "domains", "url", "columns")}
    return json.dumps(ident, sort_keys=True, default=str)


def _expand(c, plan, emit):
    """Turn planned scans into concrete Nimble calls, fanning out for_each over local rows."""
    calls = []
    for s in (plan.get("scans") or [])[:4]:
        kind = s.get("kind")
        if kind == "template" and s.get("template") not in sources.catalog():
            emit({"type": "note", "text": f"Planner picked unknown template {s.get('template')}; using Nimble Search instead."})
            s = {**s, "kind": "search", "query": " ".join(str(v) for v in (s.get("params") or {}).values()), "domains": None}
            kind = "search"
        tbl = "web_" + (re.sub(r"[^a-z0-9_]", "_", str(s.get("table") or kind).lower()).strip("_") or "t")
        binds = [{}]
        if s.get("for_each"):
            names, rows = store.run_select(c, s["for_each"])
            binds = [dict(zip(names, r)) for r in rows[:12]]
        for b in binds:
            call = _fill({k: s.get(k) for k in ("kind", "template", "params", "query", "domains", "url", "columns")}, b)
            if s.get("url"):  # values placed inside a URL must be URL-encoded
                call["url"] = _fill(s["url"], {k: quote_plus(str(v)) for k, v in b.items()})
            call.update(tbl=tbl, bind=b)
            what = call.get("template") or (",".join(call.get("domains") or []) or call.get("url") or "web")
            arg = call.get("query") or " ".join(str(v) for v in (call.get("params") or {}).values()) or ""
            call["label"] = f"{what}: {arg}".strip(": ")
            calls.append(call)
    return calls


def _scan_all(c, tx, calls, emit, chaos, max_age_s, as_of, stats):
    """Run every call in parallel. Failures are reported to Sentry, then repaired."""
    done = []  # (call, rows, fetched_at, fresh)
    todo = []
    for i, call in enumerate(calls):
        call["id"] = i
        emit({"type": "scan_start", "id": i, "label": call["label"], "table": call["tbl"], "kind": call["kind"]})
        if as_of or max_age_s:
            rows, ts = store.load_obs(c, _key(call), max_age_s=max_age_s, as_of=as_of)
            if rows:
                emit({"type": "scan_done", "id": i, "rows": len(rows), "endpoint": "tiger snapshot", "ms": 0, "cached": str(ts)[:19]})
                done.append((call, rows, ts, False))
                continue
            if as_of:
                emit({"type": "scan_fail", "id": i, "error": "no snapshot at or before that time", "final": True})
                continue
        todo.append(call)

    chaos_id = next((x["id"] for x in todo if x["kind"] == "template"), todo[0]["id"] if todo else None) if chaos else None

    def work(call):
        t = time.time()
        span = tx.start_child(op="gen_ai.execute_tool", name=f"execute_tool nimble.{call['kind']}")
        span.set_data("gen_ai.tool.name", f"nimble.{call['kind']}")
        span.set_data("gen_ai.tool.input", call["label"])
        try:
            rows, endpoint = sources.scan(call, chaos=(call["id"] == chaos_id and not call.get("repaired")))
            span.set_data("gen_ai.tool.output", f"{len(rows)} rows")
            return {"rows": rows, "endpoint": endpoint, "ms": int((time.time() - t) * 1000)}
        except Exception as e:
            span.set_status("internal_error")
            return {"error": e, "ms": int((time.time() - t) * 1000)}
        finally:
            span.finish()

    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = {pool.submit(work, call): call for call in todo}
        while pending:
            finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for f in finished:
                call, out = pending.pop(f), f.result()
                if "error" not in out:
                    stats[out["endpoint"]] = stats.get(out["endpoint"], 0) + 1
                    emit({"type": "scan_done", "id": call["id"], "rows": len(out["rows"]), "endpoint": out["endpoint"],
                          "ms": out["ms"], "repaired": bool(call.get("repaired"))})
                    done.append((call, out["rows"], dt.datetime.now(dt.timezone.utc), True))
                    continue
                err = out["error"]
                with sentry_sdk.new_scope() as scope:  # one Sentry issue per failing web table
                    scope.set_tag("web_table", call["tbl"])
                    scope.set_tag("nimble.kind", call["kind"])
                    scope.set_tag("nimble.template", call.get("template") or "-")
                    scope.set_context("scan", {"label": call["label"], "params": call.get("params"), "query": call.get("query")})
                    scope.fingerprint = ["scan-failure", call["kind"], call.get("template") or "-", type(err).__name__]
                    event_id = sentry_sdk.capture_exception(err)
                fix = None if call.get("repaired") else (sources.repair_call(call) or {**call})
                emit({"type": "scan_fail", "id": call["id"], "error": f"{type(err).__name__}: {err}"[:200],
                      "sentry": event_id, "final": fix is None, "repair": fix and fix["label"]})
                if fix:
                    fix.update(id=call["id"], repaired=True)
                    pending[pool.submit(work, fix)] = fix
    return done


def run(question, emit, chaos=False, max_age_s=None, as_of=None):
    with sentry_sdk.isolation_scope() as scope:
        scope.set_tag("question", question[:180])
        with sentry_sdk.start_transaction(op="gen_ai.invoke_agent", name="invoke_agent OuterJoin") as tx:
            tx.set_data("gen_ai.agent.name", "OuterJoin")
            tx.set_data("gen_ai.request.model", llm.MODEL)
            try:
                _run(tx, question, emit, chaos, max_age_s, as_of)
            except Exception as e:
                emit({"type": "error", "error": f"{type(e).__name__}: {e}"[:400], "sentry": sentry_sdk.capture_exception(e)})


def _run(tx, question, emit, chaos, max_age_s, as_of):
    t0 = time.time()
    stats = {}
    with store.connect() as c:
        local = store.local_tables(c)
        local_text = "\n".join(store.describe(c, n, cols) for n, cols in local.items())

        # 1. plan
        emit({"type": "stage", "stage": "plan"})
        if llm.live():
            plan = llm.ask_json(llm.PLAN.format(q=question, local=local_text, catalog=sources.catalog_text()), fast=True)
        else:
            plan = _mock_plan(question, local)
        emit({"type": "plan", "plan": plan})

        # 2. scan
        emit({"type": "stage", "stage": "scan"})
        calls = _expand(c, plan, emit)
        if not calls:
            raise RuntimeError("the plan produced no scans")
        done = _scan_all(c, tx, calls, emit, chaos, max_age_s, as_of, stats)

        # 3. bind: land rows in Tiger, build typed tables
        emit({"type": "stage", "stage": "bind"})
        by_table = {}
        for call, rows, ts, fresh in done:
            if fresh:
                store.save_obs(c, uuid.uuid4(), _key(call), call["tbl"], call["kind"], rows)
            by_table.setdefault(call["tbl"], []).extend({**r, "_fetched_at": ts} for r in rows)
        if not by_table:
            raise RuntimeError("every scan failed, nothing to query")
        tables = {}
        for tbl, rows in by_table.items():
            tables[tbl] = store.materialize(c, tbl, rows)
            emit({"type": "table", "table": tbl, "rows": len(rows), "columns": [n for n, _ in tables[tbl]]})

        # 4. execute, with a repair loop on SQL errors
        emit({"type": "stage", "stage": "execute"})
        schemas = local_text + "\n" + "\n".join(store.describe(c, n, cols) for n, cols in tables.items())
        feedback = ""
        for attempt in range(3):
            if llm.live():
                sql = llm.ask_json(llm.SQL.format(q=question, schemas=schemas, feedback=feedback))["sql"]
            else:
                sql = _mock_sql(tables)
            emit({"type": "sql", "sql": sql, "attempt": attempt + 1})
            try:
                with tx.start_child(op="db.query", name=sql[:120]):
                    names, rows = store.run_select(c, sql)
                break
            except Exception as e:
                event_id = sentry_sdk.capture_exception(e)
                emit({"type": "sql_fail", "error": str(e)[:300], "sentry": event_id})
                feedback = f"Your previous attempt failed.\nSQL: {sql}\nError: {e}\nFix it."
                if attempt == 2 or not llm.live():
                    raise
        try:  # optional: write the answer back to Snowflake
            import snowflake_sync
            if snowflake_sync.enabled():
                snowflake_sync.push(question, names, rows)
                emit({"type": "note", "text": f"Wrote {len(rows)} result rows back to Snowflake."})
        except Exception as ex:
            sentry_sdk.capture_exception(ex)
            emit({"type": "note", "text": f"Snowflake write-back skipped: {str(ex)[:120]}"})
        emit({"type": "result", "columns": names, "rows": rows, "ms": int((time.time() - t0) * 1000),
              "nimble": stats, "mock": {"nimble": not sources.live(), "gemini": not llm.live()}})
