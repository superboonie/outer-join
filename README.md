# OuterJoin

SQL over the live web. Every website is a table you can join to your own data.

Ask a question in English. Gemini plans it, Nimble fetches the web tables in parallel,
and Postgres runs the join. Every result row links back to the page it came from.

## Run it (about 5 minutes)

Needs Python 3.10+.

    pip install -r requirements.txt
    cp .env.example .env        # paste your keys
    python check.py             # tests each key, prints what came back
    uvicorn app:app --port 8000
    open http://localhost:8000

Any key you leave blank runs in MOCK mode with a red banner. `DATABASE_URL` (any Postgres) is required.

Keys:
- Nimble: API key from your Nimble account or the sponsor booth.
- Gemini: API key from Google AI Studio. If `gemini-flash-latest` is rejected, set `GEMINI_MODEL` to a model your key lists.
- Tiger: create a free service, copy the connection string into `DATABASE_URL`.
- Sentry: create a Python project, copy the DSN.

## What each file does

| File | Job |
|---|---|
| `engine.py` | The query engine: plan, scan, bind, execute, repair |
| `sources.py` | All Nimble calls: template catalog, template run, search, extract, template generation |
| `store.py` | Tiger: observation hypertable, typed temp tables, read-only SQL executor |
| `llm.py` | Gemini prompts: planner, SQL writer, page-to-rows extractor |
| `app.py` | FastAPI server with a streaming endpoint, Sentry setup |
| `static/index.html` | The demo screen |
| `snowflake_sync.py` | Optional Snowflake pull and write-back |
| `check.py` | Key and connectivity test |

## How Nimble is used

1. **The catalog is Nimble's.** `extract.templates.list` is our `SHOW TABLES`: 600 web tables at the time of the hackathon.
2. **Table scans are template runs.** `extract.templates.run` resolves the page for each scan.
3. **Bind joins.** For "which of MY products...", the engine runs one Nimble call per local row, in parallel, and tags each web row with the local key it was fetched for. That is a real distributed-database join strategy, running over the web.
4. **Self-repair through Extract.** When a template's parser returns nothing, the engine fetches the same URL with `extract.run` and Gemini shapes the page into typed rows.
5. **Any site without a template is still a table.** `search` with `include_domains` gives rows from any domain.
6. **New tables on demand (experimental).** `extract.templates.generations.create` builds a template from a URL and a sentence.

## The rest of the stack

- **Gemini**: planner with JSON output, SQL writer with an error-feedback loop, page-to-rows extraction.
- **Postgres** (Render): every fetched row is stored with a timestamp in `web_obs`, typed temp tables are built per query, and model-written SQL runs in a read-only transaction.
- **Hosting**: `render.yaml` creates the web service and database in one step. Live at https://outerjoin.onrender.com

The code also contains optional, unused-in-the-demo hooks for a TimescaleDB hypertable, Sentry tracing and a Snowflake sync. None of them were enabled for the hackathon deployment.

## Known limits

- The Amazon search template returned an empty parse during the hackathon, so Amazon rows come from the Extract-plus-Gemini repair path.
- Prices are whatever Amazon's search page showed at fetch time; matching a listing to the exact product is done by Gemini and can be wrong.
- "Create a table from any URL" and `snowflake_sync.py` are untested.
- The seed rows in `my_products` are demo data.
- `/api/debug*` and `/api/run*` are diagnostic endpoints added during the event.
