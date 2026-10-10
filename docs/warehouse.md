# Tiger Data and Snowflake

Tradio copies its cached results into both databases with one script,
`tradio/warehouse.py`. Neither one is needed for the demo: the replay page still reads
the files in `web/data/tradio/`, so it runs offline without any keys.

| Database | What it holds | Why |
|---|---|---|
| **Tiger Data** (TimescaleDB) | Every 1-minute Kalshi quote and every trade around each speech, stored as hypertables (`tradio_quotes`, `tradio_trades`) | Time-series questions, like "how far had reacting contracts moved 5, 10, 30 minutes after T0" |
| **Snowflake** | One row per speech, per (speech, contract) reaction, per scored paragraph, and per learner prediction | Questions across all 206 events, like reaction rate by speaker role and by surprise size, speed of absorption, and the learner's Brier scores |

Both get all six tables, so either one can answer either kind of question.

## Keys (gitignored, never in git or chat)

- `tigerdb.txt`: one line, the connection string from Tiger Cloud (service, then
  Connect). The repo already uses this file for `tiger_store.py`.
- `snowflake.txt`: one `key=value` per line:

  ```
  account=<org>-<account>
  user=<user>
  password=<password or programmatic access token>
  warehouse=COMPUTE_WH
  database=TRADIO
  schema=PUBLIC
  ```

  If your account signs in through the browser (as in the VS Code extension's
  `authenticator = "externalbrowser"`), put `authenticator=externalbrowser` instead of
  `password=`. Each command then opens the Snowflake sign-in page once.
  The database and schema have to exist already. You can also use env vars, like
  `SNOWFLAKE_ACCOUNT`.

## Run

```
pip install -r requirements.txt
python -m tradio.warehouse export            # CSVs in tradio/data/warehouse/, no keys needed
python -m tradio.warehouse tiger             # load into Tiger Data
python -m tradio.warehouse tiger-query       # absorption curve by verdict, 5-minute buckets
python -m tradio.warehouse snowflake         # load into Snowflake
python -m tradio.warehouse snowflake-query   # runs tradio/sql/snowflake_insights.sql
```

Running a load again replaces those events' rows, so it is safe to rerun after a
rebuild. Quotes and trades for the backfilled history speeches are loaded only where
`python -m tradio.history analyze` rebuilt their timelines locally, because those files
are gitignored.
