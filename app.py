"""OuterJoin web server.  Run:  uvicorn app:app --port 8000"""
import json
import os
import queue
import threading

from dotenv import load_dotenv

load_dotenv()

import sentry_sdk  # noqa: E402

if os.getenv("SENTRY_DSN"):
    integrations = []
    try:  # auto-traces every Gemini call as a gen_ai span
        from sentry_sdk.integrations.google_genai import GoogleGenAIIntegration
        integrations.append(GoogleGenAIIntegration())
    except Exception:
        pass
    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0, send_default_pii=True, integrations=integrations)

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import engine  # noqa: E402
import llm  # noqa: E402
import sources  # noqa: E402
import store  # noqa: E402

app = FastAPI(title="OuterJoin")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_ERROR = None
HYPER = False
try:
    HYPER = store.init()
except KeyError:
    DB_ERROR = "DATABASE_URL is not set. Add your Tiger connection string and restart."
except Exception as e:  # start anyway so the page can say what is wrong
    DB_ERROR = f"Could not connect to the database: {str(e)[:200]}"


class Query(BaseModel):
    question: str
    chaos: bool = False
    max_age_s: int | None = None
    as_of: str | None = None


class NewTable(BaseModel):
    name: str
    url: str
    prompt: str


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/status")
def status():
    if DB_ERROR:
        return {"db_error": DB_ERROR, "nimble": sources.live(), "gemini": llm.live(), "sentry": bool(os.getenv("SENTRY_DSN")),
                "hypertable": False, "model": llm.MODEL, "local": {}, "observations": 0, "nimble_calls": {}, "web_tables": []}
    with store.connect() as c:
        local = {n: [col for col, _ in cols] for n, cols in store.local_tables(c).items()}
        obs = c.execute("SELECT count(*) FROM web_obs").fetchone()[0]
    cat = sources.catalog()
    return {"nimble": sources.live(), "gemini": llm.live(), "sentry": bool(os.getenv("SENTRY_DSN")), "hypertable": HYPER,
            "model": llm.MODEL, "local": local, "observations": obs, "nimble_calls": sources.calls,
            "web_tables": [{"name": n, "domain": t["domain"], "params": list(t["params"])} for n, t in cat.items()]}


@app.post("/api/query")
def query(q: Query):
    if DB_ERROR:
        return StreamingResponse(iter([json.dumps({"type": "error", "error": DB_ERROR}) + "\n"]), media_type="application/x-ndjson")
    events = queue.Queue()

    def job():
        try:
            engine.run(q.question, events.put, chaos=q.chaos, max_age_s=q.max_age_s, as_of=q.as_of)
        finally:
            events.put(None)

    threading.Thread(target=job, daemon=True).start()

    def stream():
        while (e := events.get()) is not None:
            yield json.dumps(e, default=str) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/tables")
def new_table(t: NewTable):
    if not sources.live():
        return JSONResponse({"error": "Set NIMBLE_API_KEY to generate templates."}, status_code=400)
    try:
        return sources.create_table(t.name, t.url, t.prompt)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return JSONResponse({"error": str(e)[:400]}, status_code=500)


def _shape(o, d=0):
    """Structure of a response without the bulk: keys, list lengths, short strings."""
    if isinstance(o, dict):
        if d >= 6:
            return f"dict({len(o)})"
        return {k: _shape(v, d + 1) for k, v in list(o.items())[:40]}
    if isinstance(o, list):
        return {"_list_len": len(o), "_first": _shape(o[0], d + 1) if o else None}
    if isinstance(o, str):
        return o[:70]
    return o


@app.get("/api/debug")
def debug(template: str = "amazon_serp", keyword: str = "airpods pro", n: str = ""):
    """Temporary diagnostic: what does a template run actually return?"""
    import httpx
    out = {}
    key = llm.clean_key("NIMBLE_API_KEY")
    try:
        r = httpx.post("https://sdk.nimbleway.com/v2/extract/templates/run", timeout=120,
                       headers={"Authorization": f"Bearer {key}"}, json={"template": template, "params": {"keyword": keyword}})
        out["raw_status"] = r.status_code
        body = r.json()
        out["raw_shape"] = _shape(body)
    except Exception as e:
        out["raw_error"] = repr(e)[:300]
    try:
        res = sources.client().extract.templates.run(template=template, params={"keyword": keyword}).to_dict()
        out["sdk_shape"] = _shape(res)
        out["find_rows_count"] = len(sources.find_rows((res.get("data") or {}).get("parsing")))
    except Exception as e:
        out["sdk_error"] = repr(e)[:300]
    try:
        meta = sources.client().extract.templates.get(template).to_dict()
        pv = meta.get("published_version") or meta
        out["input_schema"] = pv.get("input_schema")
        out["meta_keys"] = list(meta.keys())
    except Exception as e:
        out["meta_error"] = repr(e)[:300]
    return out


@app.get("/api/debug2")
def debug2(keyword: str = "airpods pro", n: str = ""):
    """Temporary diagnostic: which Nimble route returns product rows with prices?"""
    from concurrent.futures import ThreadPoolExecutor
    c = sources.client()
    url = "https://www.amazon.com/s?k=" + keyword.replace(" ", "+")

    def tpl(**kw):
        res = c.extract.templates.run(**kw).to_dict()
        d = res.get("data") or {}
        return {"parsing": _shape(d.get("parsing")), "markdown_len": len(d.get("markdown") or ""),
                "markdown_head": (d.get("markdown") or "")[:400], "driver": (res.get("metadata") or {}).get("driver")}

    def srch(**kw):
        res = c.search(**kw).to_dict()
        rs = res.get("results") or []
        return {"count": len(rs), "first": _shape(rs[0]) if rs else None, "second": _shape(rs[1]) if len(rs) > 1 else None,
                "answer": (res.get("answer") or "")[:200]}

    def ext(**kw):
        res = c.extract.run(**kw).to_dict()
        d = res.get("data") or {}
        md = d.get("markdown") or ""
        return {"markdown_len": len(md), "markdown_head": md[:300], "markdown_mid": md[3000:3600],
                "parsing": _shape(d.get("parsing")), "driver": (res.get("metadata") or {}).get("driver")}

    jobs = {
        "tpl_country": lambda: tpl(template="amazon_serp", params={"keyword": keyword, "country": "US"}),
        "tpl_zip": lambda: tpl(template="amazon_serp", params={"keyword": keyword, "zip_code": "10001"}, localization=True),
        "tpl_markdown": lambda: tpl(template="amazon_serp", params={"keyword": keyword}, formats=["markdown"]),
        "tpl_page1": lambda: tpl(template="amazon_serp", params={"keyword": keyword, "page": 1}),
        "search_domain": lambda: srch(query=keyword + " price", include_domains=["amazon.com"], max_results=5),
        "search_shopping": lambda: srch(query=keyword, focus="shopping", max_results=5),
        "search_plain": lambda: srch(query=keyword + " price amazon", max_results=5),
        "extract_md": lambda: ext(url=url, formats=["markdown"]),
        "extract_render": lambda: ext(url=url, formats=["markdown"], render=True),
    }
    out = {"names": [k for k in sources.catalog() if any(w in k for w in ("amazon", "walmart", "shopping", "ebay", "target", "best_buy", "google_s", "google_m")) and "community" not in k][:60]}

    def run(item):
        k, f = item
        try:
            return k, f()
        except Exception as e:
            return k, {"error": repr(e)[:300]}

    with ThreadPoolExecutor(max_workers=9) as pool:
        out.update(dict(pool.map(run, jobs.items())))
    return out


def _shape(o, depth=0):
    """Structure of a JSON value without the bulk, for debugging response formats."""
    if depth > 6:
        return "..."
    if isinstance(o, dict):
        return {k: _shape(v, depth + 1) for k, v in list(o.items())[:40]}
    if isinstance(o, list):
        return [f"list[{len(o)}]"] + ([_shape(o[0], depth + 1)] if o else [])
    if isinstance(o, str):
        return o[:70]
    return o


@app.get("/api/debug/template")
def debug_template(template: str = "amazon_serp", key: str = "keyword", value: str = "airpods pro"):
    """Show the shape of what Nimble returns for one template run, via raw HTTP and via the SDK."""
    import httpx
    out = {"template": template, "params": {key: value}}
    try:
        r = httpx.post("https://sdk.nimbleway.com/v2/extract/templates/run", timeout=120,
                       headers={"Authorization": "Bearer " + llm.clean_key("NIMBLE_API_KEY")},
                       json={"template": template, "params": {key: value}})
        out["raw_status"] = r.status_code
        try:
            out["raw_shape"] = _shape(r.json())
        except Exception:
            out["raw_text"] = r.text[:600]
    except Exception as e:
        out["raw_error"] = repr(e)[:300]
    try:
        res = sources.client().extract.templates.run(template=template, params={key: value}).to_dict()
        out["sdk_shape"] = _shape(res)
        rows = sources.find_rows((res.get("data") or {}).get("parsing"))
        out["find_rows"] = {"count": len(rows), "first": _shape(rows[0]) if rows else None}
    except Exception as e:
        out["sdk_error"] = repr(e)[:300]
    return out


@app.get("/api/debug/template_info")
def debug_template_info(template: str = "amazon_serp"):
    try:
        return _shape(sources.client().extract.templates.get(template).to_dict())
    except Exception as e:
        return {"error": repr(e)[:300]}


@app.get("/api/run")
def run_get(q: str, chaos: bool = False):
    """Same as POST /api/query but returns every event at once."""
    events = []
    engine.run(q, events.append, chaos=chaos)
    for e in events:
        if e.get("type") == "result":
            e["rows"] = e["rows"][:8]
    return events
