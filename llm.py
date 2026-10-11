"""Gemini: the planner and the SQL writer."""
import json
import os
import time

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
_client = None
_fast_ok = True


def clean_key(name):
    """Pasted keys often pick up spaces, line breaks or a long dash. Strip those."""
    v = (os.getenv(name) or "").strip().strip('"').strip("'")
    return v.replace("\u2013", "-").replace("\u2014", "-").replace(" ", "").replace("\n", "")


def live():
    return bool(clean_key("GEMINI_API_KEY"))


def ask_json(prompt, fast=False):
    """One JSON answer from Gemini. fast=True skips the model's thinking phase for bulk extraction."""
    global _client, _fast_ok
    from google import genai
    from google.genai import types
    if _client is None:
        _client = genai.Client(api_key=clean_key("GEMINI_API_KEY"))
    base = dict(response_mime_type="application/json", temperature=0)
    for attempt in range(3):
        try:
            if fast and _fast_ok:
                try:
                    cfg = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0), **base)
                    return json.loads(_client.models.generate_content(model=MODEL, contents=prompt, config=cfg).text)
                except Exception as e:
                    if "thinking" not in str(e).lower():
                        raise
                    _fast_ok = False  # this model cannot turn thinking off
            r = _client.models.generate_content(model=MODEL, contents=prompt, config=types.GenerateContentConfig(**base))
            return json.loads(r.text)
        except Exception as e:
            if attempt == 2 or not any(t in str(e) for t in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")):
                raise
            time.sleep(6 * (attempt + 1))  # rate limit or overload: wait and retry


PLAN = """You are the query planner for OuterJoin, a SQL engine whose tables are live websites.

User question: {q}

Local tables (Postgres, already loaded):
{local}

Web tables you can scan. Each scan is one live Nimble call:
{catalog}

Return JSON: {{"scans": [...], "notes": "one sentence on the plan"}}
A scan is one of:
  {{"table": "snake_name", "kind": "template", "template": "<exact name above>", "params": {{...}}}}
  {{"table": "snake_name", "kind": "search", "query": "...", "domains": ["example.com"]}}
  {{"table": "snake_name", "kind": "url", "url": "https://...", "columns": ["col_a", "col_b"]}}
Use "search" for sites with no template and "url" for one specific page.
Optional "for_each": a SELECT over local tables. The scan then runs once per returned row,
and "{{col}}" placeholders in params or query are filled from that row. Use it whenever the
question joins web data to local rows. Add LIMIT 12 to for_each.
Rules: 1 to 4 scans. Prefer templates. Push the user's filters into params or query.
Only use param names listed for the template. Never invent template names."""

SQL = """Write ONE PostgreSQL SELECT that answers: {q}

Tables available:
{schemas}

Rules:
- Use only these tables and columns.
- Web tables carry _source (the URL the row came from) and _fetched_at.
- Web tables scanned with for_each also carry _bind_<col> columns holding the local value
  the row was fetched for. Join to the local table on those with plain equality.
- When one local row matches many web rows, keep the best one with JOIN LATERAL ... LIMIT 1.
- Always select _source from each web table you use (alias source, source_2, ...).
- Columns typed double precision are already numbers. Use ILIKE for fuzzy text.
- If a web table has a boolean column named match, require match = true: it marks listings that are
  the same product as the local row, not accessories. Ignore rows where sponsored is true if that column exists.
- Never reference a column that is not listed. If the ideal column is missing, use the closest one.
- LIMIT 50.
{feedback}
Return JSON: {{"sql": "..."}}"""

EXTRACT = """Extract rows from this web page as JSON.
Wanted columns: {cols}
Return JSON: {{"rows": [{{...}}]}} using exactly those column names. Numbers as numbers.
Use null when a value is absent. At most 40 rows. Do not invent values.

PAGE:
{page}"""
