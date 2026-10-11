"""Tiger Data (Postgres + TimescaleDB): observation history and the SQL executor."""
import decimal
import json
import os
import re

import psycopg
from psycopg.types.json import Jsonb

SEED = [  # demo "our catalog" rows. Replace with your own or load from Snowflake.
    ("SKU-001", "Apple AirPods Pro 2", 239.00),
    ("SKU-002", "Sony WH-1000XM5 headphones", 379.00),
    ("SKU-003", "Kindle Paperwhite 16GB", 159.99),
    ("SKU-004", "Instant Pot Duo 6 quart", 99.95),
    ("SKU-005", "Logitech MX Master 3S mouse", 109.99),
    ("SKU-006", "Anker 737 power bank", 139.99),
]
NUMERIC_HINT = re.compile(r"price|cost|amount|rating|review|count|score|stars|total|qty|rank")
NUMERIC_STR = re.compile(r"^\$?\s*-?[\d,]+(\.\d+)?$")


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def init():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS web_obs (
            ts timestamptz NOT NULL DEFAULT now(), scan_id uuid NOT NULL, scan_key text NOT NULL,
            tbl text, endpoint text, row jsonb)""")
        try:  # hypertable on Tiger; plain table on vanilla Postgres
            c.execute("SELECT create_hypertable('web_obs', 'ts', if_not_exists => TRUE, migrate_data => TRUE)")
            hyper = True
        except Exception:
            hyper = False
        c.execute("CREATE INDEX IF NOT EXISTS web_obs_key ON web_obs (scan_key, ts DESC)")
        c.execute("CREATE TABLE IF NOT EXISTS my_products (sku text PRIMARY KEY, name text, our_price double precision)")
        if c.execute("SELECT count(*) FROM my_products").fetchone()[0] == 0:
            c.cursor().executemany("INSERT INTO my_products VALUES (%s, %s, %s)", SEED)
        return hyper


def local_tables(c):
    """Every base table in public except our own history table."""
    out = {}
    q = """SELECT table_name, column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name <> 'web_obs' ORDER BY table_name, ordinal_position"""
    for t, col, typ in c.execute(q):
        out.setdefault(t, []).append((col, typ))
    return out


def describe(c, name, cols):
    sample = c.execute(f'SELECT * FROM "{name}" LIMIT 2').fetchall()
    lines = [f"{name}(" + ", ".join(f"{n} {t}" for n, t in cols) + ")"]
    for r in sample:
        lines.append("  e.g. " + json.dumps([str(v)[:70] if v is not None else None for v in r]))
    return "\n".join(lines)


def save_obs(c, scan_id, scan_key, tbl, endpoint, rows):
    c.cursor().executemany(
        "INSERT INTO web_obs (scan_id, scan_key, tbl, endpoint, row) VALUES (%s, %s, %s, %s, %s)",
        [(scan_id, scan_key, tbl, endpoint, Jsonb(r)) for r in rows])


def load_obs(c, scan_key, max_age_s=None, as_of=None):
    """Latest stored snapshot for a scan. Returns (rows, fetched_at) or (None, None)."""
    cond, args = "", [scan_key]
    if as_of:
        cond, args = "AND ts <= %s", [scan_key, as_of]
    elif max_age_s:
        cond, args = "AND ts >= now() - make_interval(secs => %s)", [scan_key, max_age_s]
    head = c.execute(f"SELECT scan_id, ts FROM web_obs WHERE scan_key = %s {cond} ORDER BY ts DESC LIMIT 1", args).fetchone()
    if not head:
        return None, None
    rows = [r[0] for r in c.execute("SELECT row FROM web_obs WHERE scan_id = %s", [head[0]])]
    return rows, head[1]


def _col(name):
    raw = str(name).lower()
    s = re.sub(r"[^a-z0-9_]", "_", raw).strip("_") or "col"
    if raw.startswith("_"):  # keep our own _source / _bind_* / _fetched_at markers
        return ("_" + s)[:50]
    return ("c_" + s if s[0].isdigit() else s)[:50]


def flatten(row):
    out = {}
    for k, v in row.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[_col(f"{k}_{k2}")] = v2 if not isinstance(v2, (dict, list)) else json.dumps(v2)[:500]
        elif isinstance(v, list):
            out[_col(k)] = json.dumps(v)[:500]
        else:
            out[_col(k)] = v
    return out


def _num(v):
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def materialize(c, name, rows, temp=True):
    """Turn rows into a typed table (temp per query by default). Returns [(col, type)]."""
    flat = [flatten(r) for r in rows]
    fill = {}
    for r in flat:
        for k, v in r.items():
            if v is not None and v != "":
                fill[k] = fill.get(k, 0) + 1
    keys = sorted(fill, key=lambda k: (not k.startswith("_"), -fill[k]))[:40]
    cols = []
    for k in keys:
        vals = [r.get(k) for r in flat if r.get(k) not in (None, "")]
        if all(isinstance(v, bool) for v in vals):
            typ = "boolean"
        elif k == "_fetched_at":
            typ = "timestamptz"
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            typ = "double precision"
        elif NUMERIC_HINT.search(k) and all(isinstance(v, (int, float)) or NUMERIC_STR.match(str(v).strip()) for v in vals):
            typ = "double precision"
        else:
            typ = "text"
        cols.append((k, typ))
    c.execute(f'DROP TABLE IF EXISTS "{name}"')
    c.execute(f'CREATE {"TEMP " if temp else ""}TABLE "{name}" (' + ", ".join(f'"{k}" {t}' for k, t in cols) + ")")

    def cast(v, t):
        if v is None or v == "":
            return None
        if t == "double precision":
            return _num(v)
        if t == "boolean":
            return bool(v)
        return str(v) if t == "text" else v

    c.cursor().executemany(
        f'INSERT INTO "{name}" VALUES (' + ", ".join(["%s"] * len(cols)) + ")",
        [[cast(r.get(k), t) for k, t in cols] for r in flat])
    return cols


def _plain(v):
    if isinstance(v, (int, float, bool, type(None))):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    return str(v)


def run_select(c, sql):
    """Run model-written SQL read-only. Web content can't talk its way into a write."""
    sql = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", sql) or ";" in sql:
        raise ValueError("only a single SELECT is allowed")
    with c.transaction():
        c.execute("SET TRANSACTION READ ONLY")
        c.execute("SET LOCAL statement_timeout = '15s'")
        cur = c.execute(sql)
        names = [d.name for d in cur.description]
        rows = [[_plain(v) for v in r] for r in cur.fetchmany(200)]
    return names, rows
