"""Optional Snowflake link. NOT tested against a real account: try it before relying on it.

  python snowflake_sync.py pull MY_DB.MY_SCHEMA.PRODUCTS   -> local table sf_products you can join to the web
  Every query result is also written back to OUTERJOIN_RESULTS when the env vars below are set.

Needs: pip install snowflake-connector-python
Env:   SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA
"""
import json
import os
import re
import sys


def enabled():
    return bool(os.getenv("SNOWFLAKE_ACCOUNT"))


def _conn():
    import snowflake.connector
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"], password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"), database=os.getenv("SNOWFLAKE_DATABASE"), schema=os.getenv("SNOWFLAKE_SCHEMA"))


def pull(table, limit=500):
    import store
    if not re.fullmatch(r"[\w.]+", table):
        raise ValueError("table must look like DB.SCHEMA.NAME")
    with _conn() as sf:
        cur = sf.cursor()
        cur.execute(f"SELECT * FROM {table} LIMIT {int(limit)}")
        names = [d[0].lower() for d in cur.description]
        rows = [dict(zip(names, [float(v) if type(v).__name__ == "Decimal" else v for v in r])) for r in cur.fetchall()]
    local = "sf_" + table.split(".")[-1].lower()
    with store.connect() as c:
        store.materialize(c, local, rows, temp=False)
    return local, len(rows)


def push(question, columns, rows):
    with _conn() as sf:
        cur = sf.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS OUTERJOIN_RESULTS (ts TIMESTAMP_TZ, question STRING, result VARIANT)")
        for r in rows[:200]:
            cur.execute("INSERT INTO OUTERJOIN_RESULTS SELECT CURRENT_TIMESTAMP(), %s, PARSE_JSON(%s)",
                        (question, json.dumps(dict(zip(columns, r)), default=str)))


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    if len(sys.argv) == 3 and sys.argv[1] == "pull":
        name, n = pull(sys.argv[2])
        print(f"Loaded {n} rows into local table {name}. Ask OuterJoin about it.")
    else:
        print(__doc__)
