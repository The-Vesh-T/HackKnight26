"""
tradio/warehouse.py
===================
Loads Tradio's cached results into Tiger Data and Snowflake, read-only copies for
querying. The replay page never needs either: it keeps reading the files in
tradio/data/ and web/data/tradio/, so the demo still runs offline.

    python -m tradio.warehouse export            # CSVs in tradio/data/warehouse/ (no keys)
    python -m tradio.warehouse tiger             # load into Tiger Data (tigerdb.txt)
    python -m tradio.warehouse tiger-query       # absorption curve, straight from Tiger
    python -m tradio.warehouse snowflake         # load into Snowflake (snowflake.txt)
    python -m tradio.warehouse snowflake-query   # the questions in tradio/sql/snowflake_insights.sql

Who does what:
  * Tiger Data (TimescaleDB) holds the TIME SERIES: every 1-minute quote and every trade
    around each speech, as hypertables, so "what did the tape do in minute N after T0"
    is a time_bucket query.
  * Snowflake holds the EVENT TABLES for analysis across every speech: one row per event,
    per (event, contract) reaction, per scored paragraph, and per learner prediction, so
    "how often do contracts react, by speaker role and by surprise size" is one SQL query.
  Both get the same five tables, so either can answer either kind of question.

Tables (prefix tradio_, so nothing collides with tiger_store.py's signals/ticks/trades):
  tradio_events      event_id, source (hero|history), speaker, role, kind, t0, next_decision,
                     days_to_decision, policy_paragraphs, top_z, title, url
  tradio_reactions   one row per (event, market) from role 2's analysis.json
  tradio_paragraphs  role 3's scored paragraphs (hero events)
  tradio_quotes      1-minute candles (quote time = candle END), minutes_from_t0
  tradio_trades      Kalshi trades, minutes_from_t0
  tradio_predictions the learner's test-then-train predictions, long format (model, p)

Keys (gitignored, never in git or chat):
  tigerdb.txt     one line: the Tiger Cloud connection string (or env TIGER_DB_URL),
                  postgres://tsdbadmin:<password>@<host>:<port>/tsdb?sslmode=require
  snowflake.txt   key=value lines (or env SNOWFLAKE_<KEY>):
                      account=<org>-<account>
                      user=<user>
                      password=<password or programmatic access token>
                      (or, instead of password: authenticator=externalbrowser)
                      warehouse=COMPUTE_WH
                      database=TRADIO
                      schema=PUBLIC
                      role=<optional>

Each load replaces the rows of the events it loads (delete, then insert), so running it
again after a rebuild is safe. History timelines are gitignored; their quotes/trades are
loaded only where `python -m tradio.history analyze` has rebuilt them locally.
"""

import argparse
import csv
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tradio.common import DATA_DIR, parse_ts  # noqa: E402

HISTORY_DIR = os.path.join(DATA_DIR, "history")
EXPORT_DIR = os.path.join(DATA_DIR, "warehouse")
SQL_DIR = os.path.join(ROOT, "tradio", "sql")
MODEL_PATH = os.path.join(DATA_DIR, "model.json")
PREDICTION_MODELS = ("base_rate", "playbook", "logistic", "boosting", "mlp", "ensemble")

# name -> [(column, type)]. Types are written so both Postgres and Snowflake accept them.
COLUMNS = {
    "tradio_events": [
        ("event_id", "TEXT"), ("source", "TEXT"), ("speaker", "TEXT"), ("role", "TEXT"),
        ("kind", "TEXT"), ("t0", "TIMESTAMPTZ"), ("next_decision", "DATE"),
        ("days_to_decision", "INTEGER"), ("policy_paragraphs", "INTEGER"),
        ("top_z", "DOUBLE PRECISION"), ("title", "TEXT"), ("url", "TEXT")],
    "tradio_reactions": [
        ("event_id", "TEXT"), ("market", "TEXT"), ("contract", "TEXT"), ("venue", "TEXT"),
        ("liquidity", "TEXT"), ("status", "TEXT"), ("pre_mid", "DOUBLE PRECISION"),
        ("move_ticks", "DOUBLE PRECISION"), ("alarm_t", "TIMESTAMPTZ"),
        ("confirmed_by_trade", "TIMESTAMPTZ"), ("t50_min", "DOUBLE PRECISION"),
        ("t90_min", "DOUBLE PRECISION"), ("indicative", "BOOLEAN"), ("reason", "TEXT")],
    "tradio_paragraphs": [
        ("event_id", "TEXT"), ("paragraph_id", "TEXT"), ("relevant", "BOOLEAN"),
        ("stance", "DOUBLE PRECISION"), ("z", "DOUBLE PRECISION"), ("is_top", "BOOLEAN"),
        ("key_sentence", "TEXT"), ("paragraph_text", "TEXT")],
    "tradio_quotes": [
        ("t", "TIMESTAMPTZ"), ("event_id", "TEXT"), ("market", "TEXT"),
        ("minutes_from_t0", "DOUBLE PRECISION"), ("bid", "DOUBLE PRECISION"),
        ("ask", "DOUBLE PRECISION"), ("mid", "DOUBLE PRECISION"), ("imputed", "BOOLEAN"),
        ("volume", "DOUBLE PRECISION")],
    "tradio_trades": [
        ("t", "TIMESTAMPTZ"), ("event_id", "TEXT"), ("market", "TEXT"),
        ("minutes_from_t0", "DOUBLE PRECISION"), ("price", "DOUBLE PRECISION"),
        ("size", "DOUBLE PRECISION"), ("taker_side", "TEXT")],
    "tradio_predictions": [
        ("event_id", "TEXT"), ("t0", "TIMESTAMPTZ"), ("market", "TEXT"), ("label", "INTEGER"),
        ("model", "TEXT"), ("p", "DOUBLE PRECISION"), ("trained_on", "INTEGER")],
}
HYPERTABLES = ("tradio_quotes", "tradio_trades")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _minutes(t, t0) -> float:
    return round((parse_ts(t) - parse_ts(t0)).total_seconds() / 60, 3)


def _event_dirs():
    """(event_id, folder, source) for every event with an analysis.json."""
    out = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*", "analysis.json"))):
        folder = os.path.dirname(path)
        out.append((os.path.basename(folder), folder, "hero"))
    for path in sorted(glob.glob(os.path.join(HISTORY_DIR, "*", "analysis.json"))):
        folder = os.path.dirname(path)
        out.append((os.path.basename(folder), folder, "history"))
    return out


def build_tables() -> dict:
    """Every table as a list of row dicts, from the cached files only (no network)."""
    rows = {name: [] for name in COLUMNS}
    for event_id, folder, source in _event_dirs():
        analysis = _read(os.path.join(folder, "analysis.json"))
        tl_path = os.path.join(folder, "timeline.json")
        meta_path = os.path.join(folder, "meta.json")
        timeline = _read(tl_path) if os.path.isfile(tl_path) else None
        meta = _read(meta_path) if os.path.isfile(meta_path) else {}
        t0 = meta.get("t0") or (timeline or {}).get("t0")
        if not t0:
            continue
        paragraphs = ((timeline or {}).get("speech") or {}).get("paragraphs") or []
        scored = [p for p in paragraphs if p.get("z") is not None]
        top_z = (meta.get("stance") or {}).get("top_z")
        if top_z is None and scored:
            top_z = max(scored, key=lambda p: abs(p["z"]))["z"]
        rows["tradio_events"].append({
            "event_id": event_id, "source": source,
            "speaker": meta.get("speaker") or (timeline or {}).get("speaker"),
            "role": meta.get("role"), "kind": meta.get("kind", "speech"), "t0": t0,
            "next_decision": meta.get("next_decision"),
            "days_to_decision": meta.get("days_to_decision"),
            "policy_paragraphs": meta.get("policy_paragraphs",
                                          sum(1 for p in paragraphs if p.get("relevant")) or None),
            "top_z": top_z, "title": meta.get("title") or (timeline or {}).get("title"),
            "url": meta.get("url") or ((timeline or {}).get("speech") or {}).get("source_url")})
        for market, a in analysis.get("markets", {}).items():
            venue, _, ticker = market.partition(":")
            rows["tradio_reactions"].append({
                "event_id": event_id, "market": market, "contract": ticker.rsplit("-", 1)[-1],
                "venue": venue, "liquidity": a.get("liquidity"), "status": a.get("status"),
                "pre_mid": a.get("pre_mid"), "move_ticks": a.get("move_ticks"),
                "alarm_t": a.get("alarm_t"), "confirmed_by_trade": a.get("confirmed_by_trade"),
                "t50_min": a.get("t50_min"), "t90_min": a.get("t90_min"),
                "indicative": bool(a.get("indicative", venue == "polymarket")),
                "reason": a.get("reason")})
        for p in paragraphs:
            rows["tradio_paragraphs"].append({
                "event_id": event_id, "paragraph_id": p.get("id"), "relevant": p.get("relevant"),
                "stance": p.get("stance"), "z": p.get("z"), "is_top": bool(p.get("top")),
                "key_sentence": p.get("key_sentence"), "paragraph_text": p.get("text")})
        for q in (timeline or {}).get("quotes", []):
            rows["tradio_quotes"].append({
                "t": q["t"], "event_id": event_id, "market": q["market"],
                "minutes_from_t0": _minutes(q["t"], t0), "bid": q.get("bid"), "ask": q.get("ask"),
                "mid": q.get("mid"), "imputed": q.get("imputed"), "volume": q.get("volume")})
        for tr in (timeline or {}).get("trades", []):
            rows["tradio_trades"].append({
                "t": tr["t"], "event_id": event_id, "market": tr["market"],
                "minutes_from_t0": _minutes(tr["t"], t0), "price": tr.get("price"),
                "size": tr.get("size"), "taker_side": tr.get("taker_side")})
    if os.path.isfile(MODEL_PATH):
        for pred in _read(MODEL_PATH).get("predictions", []):
            for r in pred.get("rows", []):
                for model in PREDICTION_MODELS:
                    if r.get(model) is not None:
                        rows["tradio_predictions"].append({
                            "event_id": pred["event"], "t0": pred["t0"], "market": r["market"],
                            "label": r.get("label"), "model": model, "p": r[model],
                            "trained_on": pred.get("trained_on")})
    return rows


def _values(table: str, rows: list) -> list:
    cols = [c for c, _ in COLUMNS[table]]
    return [tuple(r.get(c) for c in cols) for r in rows]


def export(out_dir: str = EXPORT_DIR) -> dict:
    tables = build_tables()
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, ".gitignore"), "w") as f:
        f.write("*\n")   # generated; rebuild with `python -m tradio.warehouse export`
    for table, rows in tables.items():
        with open(os.path.join(out_dir, f"{table}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([c for c, _ in COLUMNS[table]])
            w.writerows(_values(table, rows))
    return {t: len(r) for t, r in tables.items()}


# ---------------------------------------------------------------- Tiger Data (Postgres)

def tiger_url() -> str:
    url = os.environ.get("TIGER_DB_URL", "")
    path = os.path.join(ROOT, "tigerdb.txt")
    if not url and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            url = f.read().strip()
    return url


def tiger_ddl() -> list:
    stmts = []
    for table, cols in COLUMNS.items():
        body = ", ".join(f"{c} {t}" for c, t in cols)
        stmts.append(f"CREATE TABLE IF NOT EXISTS {table} ({body})")
    stmts.append("CREATE INDEX IF NOT EXISTS tradio_quotes_event ON tradio_quotes (event_id, market, t)")
    stmts.append("CREATE INDEX IF NOT EXISTS tradio_trades_event ON tradio_trades (event_id, market, t)")
    return stmts


def load_tiger(conn, tables: dict) -> dict:
    """Create tables (hypertables where TimescaleDB is there), replace these events' rows."""
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    for stmt in tiger_ddl():
        cur.execute(stmt)
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    if cur.fetchone():
        for table in HYPERTABLES:
            cur.execute(f"SELECT create_hypertable('{table}', 't', if_not_exists => TRUE, "
                        "migrate_data => TRUE)")
    events = sorted({r["event_id"] for rows in tables.values() for r in rows})
    counts = {}
    for table, rows in tables.items():
        cur.execute(f"DELETE FROM {table} WHERE event_id = ANY(%s)", (events,))
        cols = ", ".join(c for c, _ in COLUMNS[table])
        if rows:
            execute_values(cur, f"INSERT INTO {table} ({cols}) VALUES %s",
                           _values(table, rows), page_size=1000)
        counts[table] = len(rows)
    conn.commit()
    return counts


# Mean distance of each active Kalshi contract from its last pre-T0 mid, minute by minute,
# split by role 2's verdict. This is the "absorption curve" the replay shows one event at a time.
TIGER_CURVE_SQL = """
WITH pre AS (
    SELECT DISTINCT ON (q.event_id, q.market) q.event_id, q.market, q.mid AS pre_mid
    FROM tradio_quotes q
    WHERE q.minutes_from_t0 <= 0 AND q.mid IS NOT NULL
    ORDER BY q.event_id, q.market, q.t DESC
)
SELECT r.status,
       floor(q.minutes_from_t0 / 5) * 5 AS minute,
       round(avg(abs(q.mid - pre.pre_mid))::numeric * 100, 2) AS avg_cents_moved,
       count(DISTINCT q.event_id || q.market) AS contracts
FROM tradio_quotes q
JOIN pre USING (event_id, market)
JOIN tradio_reactions r USING (event_id, market)
WHERE r.venue = 'kalshi' AND r.liquidity = 'active' AND q.minutes_from_t0 BETWEEN 0 AND 90
GROUP BY r.status, minute
ORDER BY r.status, minute
"""


# ---------------------------------------------------------------- Snowflake

SNOWFLAKE_KEYS = ("account", "user", "password", "authenticator", "warehouse", "database", "schema", "role")


def snowflake_config() -> dict:
    cfg = {}
    path = os.path.join(ROOT, "snowflake.txt")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    cfg[key.strip().lower()] = value.strip().strip('"')
    for key in SNOWFLAKE_KEYS:
        env = os.environ.get(f"SNOWFLAKE_{key.upper()}")
        if env:
            cfg[key] = env
    return {k: v for k, v in cfg.items() if k in SNOWFLAKE_KEYS and v}


def snowflake_type(pg_type: str) -> str:
    return {"TIMESTAMPTZ": "TIMESTAMP_TZ", "DOUBLE PRECISION": "FLOAT"}.get(pg_type, pg_type)


def snowflake_ddl() -> list:
    stmts = []
    for table, cols in COLUMNS.items():
        body = ", ".join(f"{c} {snowflake_type(t)}" for c, t in cols)
        stmts.append(f"CREATE TABLE IF NOT EXISTS {table} ({body})")
    return stmts


def load_snowflake(conn, tables: dict) -> dict:
    cur = conn.cursor()
    for stmt in snowflake_ddl():
        cur.execute(stmt)
    events = sorted({r["event_id"] for rows in tables.values() for r in rows})
    counts = {}
    for table, rows in tables.items():
        for i in range(0, len(events), 500):
            chunk = events[i:i + 500]
            cur.execute(f"DELETE FROM {table} WHERE event_id IN ({', '.join(['%s'] * len(chunk))})",
                        chunk)
        cols = [c for c, _ in COLUMNS[table]]
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        values = _values(table, rows)
        for i in range(0, len(values), 5000):
            cur.executemany(sql, values[i:i + 5000])
        counts[table] = len(rows)
    conn.commit()
    return counts


def snowflake_queries() -> list:
    """(title, sql) pairs from tradio/sql/snowflake_insights.sql, split on '-- name:' lines."""
    with open(os.path.join(SQL_DIR, "snowflake_insights.sql"), encoding="utf-8") as f:
        text = f.read()
    out = []
    for block in text.split("-- name:")[1:]:
        title, _, sql = block.partition("\n")
        out.append((title.strip(), sql.strip().rstrip(";")))
    return out


# ---------------------------------------------------------------- CLI

def _print_rows(header, rows):
    print("  " + " | ".join(header))
    for r in rows:
        print("  " + " | ".join("" if v is None else str(v) for v in r))


def main():
    parser = argparse.ArgumentParser(description="Load Tradio results into Tiger Data / Snowflake")
    parser.add_argument("step", choices=["export", "tiger", "tiger-query", "snowflake", "snowflake-query"])
    args = parser.parse_args()

    if args.step == "export":
        for table, n in export().items():
            print(f"  {table}: {n} rows")
        print(f"wrote {EXPORT_DIR}")
        return

    if args.step.startswith("tiger"):
        url = tiger_url()
        if not url:
            raise SystemExit("No Tiger connection string: put it in tigerdb.txt or TIGER_DB_URL.")
        import psycopg2
        conn = psycopg2.connect(url)
        if args.step == "tiger":
            for table, n in load_tiger(conn, build_tables()).items():
                print(f"  {table}: {n} rows")
        else:
            cur = conn.cursor()
            cur.execute(TIGER_CURVE_SQL)
            _print_rows([d[0] for d in cur.description], cur.fetchall())
        conn.close()
        return

    cfg = snowflake_config()
    # password, or authenticator=externalbrowser (opens the Snowflake sign-in page once)
    missing = [k for k in ("account", "user") if k not in cfg]
    if "password" not in cfg and "authenticator" not in cfg:
        missing.append("password (or authenticator=externalbrowser)")
    if missing:
        raise SystemExit(f"snowflake.txt is missing {', '.join(missing)} (see the top of this file).")
    import snowflake.connector
    conn = snowflake.connector.connect(**cfg)
    if args.step == "snowflake":
        for table, n in load_snowflake(conn, build_tables()).items():
            print(f"  {table}: {n} rows")
    else:
        cur = conn.cursor()
        for title, sql in snowflake_queries():
            print(f"\n{title}")
            cur.execute(sql)
            _print_rows([d[0] for d in cur.description], cur.fetchall())
    conn.close()


if __name__ == "__main__":
    main()
