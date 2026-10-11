# OuterJoin

SQL over the live web. Every website is a table you can join to your own data.

Ask a question in English. Gemini plans it, Nimble fetches the web tables in parallel,
Tiger stores every observation, Postgres runs the join, and Sentry watches every agent call.

## Run it (about 5 minutes)

Needs Python 3.10+.

    pip install -r requirements.txt
    cp .env.example .env        # paste your keys
    python check.py             # tests each key, prints what came back
    uvicorn app:app --port 8000
    open http://localhost:8000

Any key you leave blank runs in MOCK mode with a red banner, so you can rehearse the screen
before every key arrives. Never demo with the banner showing. `DATABASE_URL` is the one key
you must have.

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

## How Nimble is used (say this to the Nimble judge)

1. **The catalog is Nimble's.** `extract.templates.list` is our `SHOW TABLES`. Every template Nimble maintains is a table, with its input schema as the table's parameters.
2. **Table scans are template runs.** `extract.templates.run` returns structured fields, so rows arrive typed.
3. **Bind joins.** For "which of MY products...", we run one template call per local row, in parallel, and tag each web row with the local key it was fetched for. That is a real distributed-database join strategy, running over the web.
4. **Any site without a template is still a table.** `search` with `include_domains` gives rows from any domain. `extract.run` plus Gemini turns one specific page into rows.
5. **New tables on demand.** `extract.templates.generations.create` builds a template from a URL and a sentence. That is `CREATE TABLE` for the web.
6. **Self-repair.** When a template scan fails, the engine retries through Nimble Search scoped to the same domain.

## The other sponsors

- **Tiger Data**: `web_obs` is a hypertable of every row ever fetched. That gives snapshot reuse ("Reuse snapshots ≤ 10 min") and time travel ("As of 30 min ago") from the same table.
- **Sentry**: each query is one trace (`gen_ai.invoke_agent`), each Nimble call is a tool span (`gen_ai.execute_tool`), Gemini calls are traced automatically. A failing web table becomes one grouped issue tagged with the table and template. The "Break an agent" switch shows this live.
- **Gemini**: planner with JSON output, SQL writer with an error-feedback loop, page-to-rows extraction.
- **Snowflake** (optional): `python snowflake_sync.py pull DB.SCHEMA.TABLE` loads a warehouse table as `sf_<table>`; results are written back to `OUTERJOIN_RESULTS`.

## 90-second demo

1. "Every AI agent here can search the web. None of them can JOIN it." Run the first example. Point at the six parallel Nimble calls, then the result table with a source link on every row.
2. Show the SQL at the bottom. "Gemini wrote this after seeing the real columns Nimble returned."
3. Tick **Break an agent mid-query**, run again. A scan fails, the repair line appears, the answer still arrives. Switch to Sentry and show the trace and the issue.
4. Set Freshness to **Reuse snapshots**, run again. Instant, served from Tiger. "Every observation is kept, so we can query the web as of any past moment."
5. If template generation works on your account: create a table from a URL a judge names, then query it.

Rehearse twice and record a backup video. Venue wifi fails.

## Known limits

- Built and tested end to end in mock mode. The live Nimble, Gemini, Sentry and Tiger calls follow the current SDKs but were not run with real keys. `check.py` is how you find out in one minute.
- `snowflake_sync.py` and "Create a table from any URL" are untested against real accounts.
- "As of" only works for scans that already ran earlier, with the same parameters.
- The seed rows in `my_products` are demo data. Replace them with your own.
