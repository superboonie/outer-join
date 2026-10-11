"""Nimble: every web table is backed by one of these calls.

  catalog()      extract.templates.list              -> the live list of web tables + their params
  scan template  extract.templates.run               -> structured rows from a known site
  scan search    search(include_domains=...)         -> rows from any site, no template needed
  scan url       extract.run(formats=["markdown"])   -> one page, Gemini shapes it into rows
  create_table   extract.templates.generations       -> a brand new template from a URL + a sentence
"""
import hashlib
import os
import threading
import time

import llm

_client = None
_catalog = None
calls = {}  # endpoint -> count, for the on-screen Nimble counter
_lock = threading.Lock()

FALLBACK_CATALOG = {  # used only if the live list call fails; params are the documented ones
    "amazon_serp": {"domain": "amazon.com", "desc": "Amazon search results", "params": {"keyword": "string"}, "required": ["keyword"]},
    "amazon_pdp": {"domain": "amazon.com", "desc": "Amazon product page", "params": {"asin": "string"}, "required": ["asin"]},
    "google_maps_search": {"domain": "google.com/maps", "desc": "Google Maps places", "params": {"query": "string"}, "required": ["query"]},
}


class EmptyScan(Exception):
    """A scan succeeded at the HTTP level but produced no usable rows."""


class ChaosInjected(Exception):
    """Deliberate failure from the demo's 'break an agent' switch."""


def live():
    return bool(os.getenv("NIMBLE_API_KEY"))


def client():
    global _client
    if _client is None:
        from nimble_python import Nimble
        _client = Nimble(api_key=os.environ["NIMBLE_API_KEY"], timeout=120.0, max_retries=1)
    return _client


def _count(endpoint):
    with _lock:
        calls[endpoint] = calls.get(endpoint, 0) + 1


def catalog(refresh=False):
    global _catalog
    if _catalog is not None and not refresh:
        return _catalog
    if not live():
        _catalog = dict(FALLBACK_CATALOG)
        return _catalog
    cat = {}
    try:
        offset = 0
        while offset < 600:
            page = client().extract.templates.list(limit=100, offset=offset).to_dict()
            _count("templates.list")
            items = page.get("items") or []
            for it in items:
                pv = it.get("published_version") or {}
                md = pv.get("metadata") or {}
                schema = pv.get("input_schema") or {}
                props = schema.get("properties") or {}
                cat[it["name"]] = {
                    "domain": md.get("domain") or "",
                    "desc": (md.get("description") or md.get("display_name") or "")[:90],
                    "params": {k: (v or {}).get("type", "string") for k, v in props.items()},
                    "required": schema.get("required") or [],
                }
            offset += 100
            if len(items) < 100:
                break
    except Exception as e:  # keep the app usable if the catalog call fails
        print("catalog fallback:", repr(e))
    _catalog = cat or dict(FALLBACK_CATALOG)
    return _catalog


def catalog_text(limit=160):
    lines = []
    for name, t in list(catalog().items())[:limit]:
        ps = ", ".join(f"{k}{'*' if k in t['required'] else ''}" for k in list(t["params"])[:8])
        lines.append(f"- {name} [{t['domain']}] {t['desc']} | params: {ps}  (* = required)")
    return "\n".join(lines)


# ---------- turning whatever a site returns into rows ----------

def find_rows(p):
    if isinstance(p, dict) and p.get("status") == "error":
        raise EmptyScan(f"parser error: {p.get('error')}")
    if isinstance(p, dict) and isinstance(p.get("entities"), (dict, list)):
        p = p["entities"]
    if isinstance(p, list):
        return [x for x in p if isinstance(x, dict)]
    if not isinstance(p, dict):
        return []
    if sum(1 for v in p.values() if not isinstance(v, (dict, list))) >= 5:
        return [p]  # a single record, e.g. a product page
    level = [p]
    for _ in range(4):  # shallowest list of records wins
        best, nxt = [], []
        for o in level:
            for v in (o.values() if isinstance(o, dict) else o):
                if isinstance(v, list):
                    ds = [x for x in v if isinstance(x, dict)]
                    if len(ds) > len(best):
                        best = ds
                    nxt.append(v[:3])
                elif isinstance(v, dict):
                    nxt.append(v)
        if best:
            return best
        level = nxt
    return [p]


def _source(row, default):
    for k in ("url", "product_url", "link", "href", "page_url", "place_url"):
        v = row.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return default or ""


def _mock_rows(label):
    h = int(hashlib.md5(label.encode()).hexdigest(), 16)
    base = 40 + h % 300
    return [{"product_name": f"[MOCK] {label} listing {i + 1}", "price": round(base * (0.9 + 0.07 * i), 2),
             "rating": round(3.9 + (h >> i) % 10 / 10, 1), "url": "https://example.com/mock"} for i in range(4)]


# ---------- the three scan kinds ----------

def scan(call, chaos=False):
    """Run one Nimble call. Returns (rows, endpoint). Raises on failure or no rows."""
    kind = call["kind"]
    if chaos and kind == "template":
        raise ChaosInjected(f"injected failure in template {call.get('template')}")
    if not live():
        time.sleep(0.4)
        rows = _mock_rows(str(call.get("label")))
        endpoint, default = "MOCK", ""
    elif kind == "template":
        res = client().extract.templates.run(template=call["template"], params=call["params"]).to_dict()
        _count("extract.templates.run")
        rows = find_rows((res.get("data") or {}).get("parsing"))
        endpoint, default = "extract.templates.run", res.get("url")
    elif kind == "search":
        kw = {"query": call["query"], "max_results": 10}
        if call.get("domains"):
            kw["include_domains"] = call["domains"]
        res = client().search(**kw).to_dict()
        _count("search")
        rows = []
        for r in res.get("results") or []:
            row = {"title": r.get("title"), "url": r.get("url"), "description": (r.get("description") or "")[:400]}
            row.update(r.get("additional_data") or {})
            rows.append(row)
        endpoint, default = "search", ""
    elif kind == "url":
        res = client().extract.run(url=call["url"], formats=["markdown"]).to_dict()
        _count("extract.run")
        md = ((res.get("data") or {}).get("markdown") or "")[:60000]
        if not md.strip():
            raise EmptyScan(f"no content at {call['url']}")
        out = llm.ask_json(llm.EXTRACT.format(cols=call.get("columns") or "whatever best describes the main list", page=md))
        rows = [r for r in out.get("rows") or [] if isinstance(r, dict)]
        endpoint, default = "extract.run", call["url"]
    else:
        raise ValueError(f"unknown scan kind {kind}")
    if not rows:
        raise EmptyScan(f"0 rows from {call.get('label')}")
    for r in rows:
        r["_source"] = _source(r, default)
        for k, v in (call.get("bind") or {}).items():
            r[f"_bind_{k}"] = v
    return rows[:40], endpoint


def repair_call(call):
    """When a template scan fails, fall back to Nimble Search scoped to the same site."""
    if call["kind"] != "template":
        return None
    t = catalog().get(call["template"], {})
    text = " ".join(str(v) for v in call["params"].values() if isinstance(v, (str, int, float)))
    domain = (t.get("domain") or "").split("/")[0].removeprefix("www.")
    if not text:
        return None
    return {**call, "kind": "search", "query": text, "domains": [domain] if domain else None,
            "label": f"search {domain or 'web'}: {text}"}


def create_table(name, url, prompt):
    """CREATE TABLE web.<name> FROM '<url>': ask Nimble to generate a new extract template."""
    c = client()
    gen = c.extract.templates.generations.create(prompt=prompt, url=url, name=name).to_dict()
    _count("extract.templates.generations")
    deadline = time.time() + 240
    while time.time() < deadline:
        if gen.get("generated_version") or gen.get("error") or any(s in str(gen.get("status")).lower() for s in ("fail", "error", "cancel")):
            break
        time.sleep(4)
        gen = c.extract.templates.generations.get(gen["id"]).to_dict()
    if not gen.get("generated_version"):
        raise RuntimeError(f"template generation did not finish: status={gen.get('status')} error={gen.get('error')}")
    catalog(refresh=True)
    v = gen["generated_version"]
    return {"name": v.get("name") or gen.get("name") or name, "status": gen.get("status"),
            "params": list(((v.get("input_schema") or {}).get("properties") or {}).keys()),
            "columns": list(((v.get("output_schema") or {}).get("properties") or {}).keys()), "summary": gen.get("summary")}
