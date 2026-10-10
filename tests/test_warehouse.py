"""Tests for tradio/warehouse.py (no Tiger or Snowflake account needed).

    python -m unittest tests.test_warehouse
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tradio import warehouse  # noqa: E402


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))

    def executemany(self, sql, values):
        self.log.append(("executemany", sql, list(values)))


class FakeConn:
    def __init__(self):
        self.log, self.committed = [], False

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        self.committed = True


class TestBuildTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = warehouse.build_tables()

    def test_every_table_has_its_columns(self):
        for table, rows in self.tables.items():
            cols = {c for c, _ in warehouse.COLUMNS[table]}
            for r in rows[:50]:
                self.assertEqual(set(r), cols, table)

    def test_hero_events_present_with_scores(self):
        events = {e["event_id"]: e for e in self.tables["tradio_events"]}
        self.assertEqual(events["bowman20250623"]["source"], "hero")
        self.assertAlmostEqual(events["bowman20250623"]["top_z"], -3.501, places=3)
        tops = [p for p in self.tables["tradio_paragraphs"]
                if p["event_id"] == "bowman20250623" and p["is_top"]]
        self.assertEqual(len(tops), 1)

    def test_quote_minutes_relative_to_t0(self):
        q = next(q for q in self.tables["tradio_quotes"]
                 if q["event_id"] == "bowman20250623" and q["t"] == "2025-06-23T14:07:00Z")
        self.assertEqual(q["minutes_from_t0"], 7.0)

    def test_reactions_split_market_id(self):
        r = next(r for r in self.tables["tradio_reactions"]
                 if r["market"] == "kalshi:KXFEDDECISION-25JUL-C25" and r["event_id"] == "bowman20250623")
        self.assertEqual((r["venue"], r["contract"], r["status"]), ("kalshi", "C25", "reacted"))


class TestSnowflake(unittest.TestCase):
    def test_load_replaces_then_inserts(self):
        tables = {t: [] for t in warehouse.COLUMNS}
        tables["tradio_events"] = [{"event_id": "e1", "t0": "2025-06-23T14:00:00Z"}]
        conn = FakeConn()
        counts = warehouse.load_snowflake(conn, tables)
        self.assertTrue(conn.committed)
        self.assertEqual(counts["tradio_events"], 1)
        kinds = [(k, sql.split()[0]) for k, sql, _ in conn.log]
        self.assertIn(("execute", "CREATE"), kinds)
        self.assertIn(("execute", "DELETE"), kinds)
        insert = next(x for x in conn.log if x[0] == "executemany" and "tradio_events" in x[1])
        self.assertEqual(insert[2][0][0], "e1")
        self.assertNotIn("TIMESTAMPTZ", " ".join(warehouse.snowflake_ddl()))

    def test_queries_file_parses(self):
        queries = warehouse.snowflake_queries()
        self.assertEqual(len(queries), 4)
        self.assertTrue(all(sql.upper().startswith("SELECT") for _, sql in queries))

    def test_config_from_file_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "snowflake.txt"), "w") as f:
                f.write("# comment\naccount = org-acct\nuser=me\nbogus=1\n")
            with mock.patch.object(warehouse, "ROOT", tmp), \
                 mock.patch.dict(os.environ, {"SNOWFLAKE_PASSWORD": "pw"}):
                cfg = warehouse.snowflake_config()
        self.assertEqual(cfg, {"account": "org-acct", "user": "me", "password": "pw"})


class TestExport(unittest.TestCase):
    def test_export_writes_csvs_and_ignores_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            counts = warehouse.export(tmp)
            self.assertEqual(set(counts), set(warehouse.COLUMNS))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "tradio_quotes.csv")))
            with open(os.path.join(tmp, ".gitignore")) as f:
                self.assertEqual(f.read().strip(), "*")


if __name__ == "__main__":
    unittest.main()
