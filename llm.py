"""Gemini: the planner and the SQL writer."""
import json
import os

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
_client = None


def clean_key(name):
    """Pasted keys often pick up spaces, line breaks or a long dash. Strip those."""
    v = (os.getenv(name) or "").strip().strip('"').strip("'")
    return v.replace("\u2013", "-").replace("\u2014", "-").replace(" ", "").replace("\n", "")


def live():
    return bool(clean_key("GEMINI_API_KEY"))


def ask_json(prompt):
    global _client
    from google import genai
    from google.genai import types
    if _client is None:
        _client = genai.Client(api_key=clean_key("GEMINI_API_KEY"))
    r = _client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
    return json.loads(r.text)


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
- LIMIT 50.
{feedback}
Return JSON: {{"sql": "..."}}"""

EXTRACT = """Extract rows from this web page as JSON.
Wanted columns: {cols}
Return JSON: {{"rows": [{{...}}]}} using exactly those column names. Numbers as numbers.
Use null when a value is absent. At most 40 rows. Do not invent values.

PAGE:
{page}"""
