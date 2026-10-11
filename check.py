"""Run this first:  python check.py   It tests each key and prints what came back."""
import os
import traceback

from dotenv import load_dotenv

load_dotenv()


def step(name, fn):
    try:
        print(f"[ OK ] {name}: {fn()}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:300]}")
        if os.getenv("DEBUG"):
            traceback.print_exc()


def tiger():
    import store
    hyper = store.init()
    return "connected, hypertable ready" if hyper else "connected, but TimescaleDB is missing (plain Postgres still works)"


def nimble_catalog():
    import sources
    cat = sources.catalog(refresh=True)
    return f"{len(cat)} templates, e.g. {list(cat)[:6]}"


def nimble_template():
    import sources
    rows, ep = sources.scan({"kind": "template", "template": "amazon_serp", "params": {"keyword": "airpods pro"}, "label": "check"})
    return f"{len(rows)} rows via {ep}; columns: {list(rows[0])[:10]}"


def nimble_search():
    import sources
    rows, ep = sources.scan({"kind": "search", "query": "airpods pro price", "domains": ["walmart.com"], "label": "check"})
    return f"{len(rows)} rows via {ep}; columns: {list(rows[0])[:8]}"


def gemini():
    import llm
    answer = llm.ask_json('Return JSON {"ok": true}')
    return f"{llm.MODEL} says {answer}"


def sentry():
    import sentry_sdk
    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"])
    eid = sentry_sdk.capture_message("OuterJoin check.py test event")
    sentry_sdk.flush(timeout=5)
    return f"sent test event {eid}; look for it in your Sentry project"


for key, checks in [("DATABASE_URL", [("Tiger", tiger)]),
                    ("NIMBLE_API_KEY", [("Nimble template catalog", nimble_catalog), ("Nimble template run", nimble_template), ("Nimble search", nimble_search)]),
                    ("GEMINI_API_KEY", [("Gemini", gemini)]),
                    ("SENTRY_DSN", [("Sentry", sentry)])]:
    if not os.getenv(key):
        print(f"[SKIP] {key} is not set")
        continue
    for name, fn in checks:
        step(name, fn)
