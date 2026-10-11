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
