# Role 1 — Market data + replay timeline

Read `CLAUDE.md` first. You own: `tradio/events.yaml`, `tradio/ingest_markets.py`,
`tradio/build_timeline.py`, `tradio/data/`, and the copies in `web/data/tradio/`.

**Your goal:** produce a correct, offline `timeline.json` for Bowman (then Waller) that
roles 2 and 3 can build on. You are the source of truth for every price on screen.

## Tasks, in order

1. **`tradio/events.yaml`** — one entry per event:
   - `bowman20250623`: speaker `michelle_bowman`, t0 `2025-06-23T14:00:00Z`,
     window 13:00–15:30 UTC, speech `https://www.federalreserve.gov/newsevents/speech/bowman20250623a.htm`
   - `waller20250717`: speaker `christopher_waller`, t0 `2025-07-17T22:30:00Z`,
     window 21:30–00:00 UTC, speech `https://www.federalreserve.gov/newsevents/speech/waller20250717a.htm`
   - Markets for both (all verified to exist):
     - Kalshi July 2025 decision, exposure `direct`: `KXFEDDECISION-25JUL-H26`, `-H25`, `-H0`, `-C25`, `-C26`
     - Kalshi 2025 cut count, exposure `partial`: `KXRATECUTCOUNT-25DEC31-T0`, `-T1`, `-T2`, `-T3`
     - Polymarket "Fed decision in July?" (slug `fed-decision-in-july`), `indicative: true`.
       YES token ids: 25 bp cut `44274863594881419064755425975616427905570818290480331177906247008783209380302`,
       no change `73793697275923646355831824753345919471011673129768736520616870696302293395247`,
       50+ bp cut `17028788563275937059830939874003546190073058810431474110375077031370800019934`,
       hike 25+ `100067677053694812730460296850221451830656965871151478092125776330632208052828`

2. **`tradio/ingest_markets.py`** — cache raw responses to `tradio/data/<event_id>/raw/`.
   No auth needed. Base `https://api.elections.kalshi.com/trade-api/v2`.
   - Trades: `GET /historical/trades?ticker=...&min_ts=...&max_ts=...&limit=1000` — newest
     first, paginate with `cursor` until empty. A page can be truncated if the window is
     big; keep windows ≤ 30 min per request if needed.
   - 1-min candles: `GET /historical/markets/{ticker}/candlesticks?start_ts=...&end_ts=...&period_interval=1`.
     Use `yes_bid.close`, `yes_ask.close`, `price.close`, `volume`. Some minutes are
     missing from the feed (e.g. 13:53 on 2025-06-23): carry forward and set `imputed: true`.
   - Polymarket: `GET https://clob.polymarket.com/prices-history?market=<token>&startTs=...&endTs=...&fidelity=1`.
     Query the window in ≤ 70-minute chunks and keep every raw response; we saw
     contradictory results across query windows. Mark these markets `indicative`.

3. **Verify candle timing** (important): compare each candle's `volume` with the sum of
   trades in the minute before vs after `end_period_ts`. Write the answer in
   `tradio/data/NOTES.md`. Expected (verify): the 14:07 candle for `-C25` holds the
   ~129,898 contracts traded 14:06:52–14:06:56.

4. **`tradio/build_timeline.py`** — merge into `tradio/data/<event_id>/timeline.json`
   (schema in `CLAUDE.md`), leaving `speech.paragraphs` empty for role 3 to fill.
   Copy the final `timeline.json` (and role 2's `analysis.json`) to
   `web/data/tradio/<event_id>/` for the page.

5. **Spot-check** (must pass): these values from our earlier pull should appear in your files.
   If any differs, trust your data, write it in `NOTES.md`, and tell the team.
   - `-H0` trade at 0.85, 2025-06-23T14:01:49Z
   - `-C25` first trade at 0.16, 14:08:43Z; 0.20 at 14:24:38Z; 0.22 at 14:29:33Z
   - `-C25` candle 14:30: bid 0.21 / ask 0.22. `-H0` candle 14:41: 0.77 / 0.78
   - Waller: `-C25` first trade at 0.05, 2025-07-17T22:35:04Z; `-H0` first 0.94 at 22:35:00Z

6. After Gate 1: give role 4 a tiny `evidence.json` per event (the raw trade/candle rows
   behind every number the page shows) for the evidence drawer.
7. After Gate 3, only if the team agrees: pull data for the next candidate events
   (Powell Jackson Hole 2025-08-22, Powell 2025-10-29 press conference) and the
   placebo windows role 2 needs.

## Done when
- Both events have `timeline.json` that validates against the schema.
- `NOTES.md` states the candle-timing answer and any spot-check differences.
- Running both scripts again from scratch reproduces the same files.

## Don't
- Don't touch trading modules or use authenticated endpoints.
- Don't smooth, fill, or "fix" prices beyond carry-forward with `imputed: true`.
