# HackKnight26 — Tradio

Read this whole file before doing anything. Then ask the user which role they are
(1, 2, 3 or 4) and read their file in `docs/team/`. Only work in the files your role owns.

## What we are building this weekend

The live desk (everything already in this repo) listens to Fed speakers and scores each
passage's stance against that speaker's own history (`stance_scorer.py`, z-scores).

**Tradio adds the other half: what the markets did next.** For a historical Fed
speech, we replay the speech release and the real Kalshi/Polymarket tape side by side,
measure how fast and how fully each related market absorbed the surprise, and refuse to
call a thin market "slow".

Hero demo (build this end to end before anything else):
- **Bowman, 2025-06-23.** Prepared text released 14:00:00 UTC (10:00 a.m. EDT). She was
  the Fed's most hawkish governor and said she would support a July cut. Kalshi July
  25 bp cut went 0.15 → 0.22 and July hold 0.86 → 0.77 over ~30–45 minutes.
- **Control: Waller, 2025-07-17.** Text released 22:30:00 UTC (6:30 p.m. EDT). A known
  dove being dovish. Same contracts moved only 1–2 cents.

Full evidence, algorithms and success criteria: the "Tradio — Evidence & Build Plan"
doc (ask Devesh for the link). This file is the short version.

## Ground rules

1. **Never invent results.** Every number shown in the UI must come from a cached data
   file we pulled. If something is unverified, label it on screen.
2. **Polymarket price history is "indicative" only.** The same contract returned
   contradictory values depending on the query window. Never use it for timing claims.
3. **Speech text arrives all at once at T0.** Bowman and Waller released prepared texts.
   Never animate the market reacting to sentences "as she spoke".
4. **Trading only through the live desk, demo money by default** (team decision,
   2026-10-09). Tradio's own scripts (`tradio/*.py`) never place orders and never import
   `kalshi_trader.py`, `polymarket_client.py`, `manual_orders.py`, `trading_common.py` or
   `control_worker.py`. Orders come only from `live.py --playbook`, which asks
   `tradio/playbook.py` which trades its learned rules allow. Use `--mode demo` (Kalshi's
   fake-money exchange) for building and the demo; real money stays behind the existing
   LIVE opt-ins and is the operator's call, never switched on in code. Don't change
   `kalshi_trader.py`, `trading_common.py` or the risk limits.
5. **No keys in git.** Keys stay in the gitignored `*.txt` files. Kalshi/Polymarket
   historical endpoints we use need no auth.
6. **Times:** data files store UTC ISO 8601 (`2025-06-23T14:00:00Z`). The UI shows ET too.
7. **Demo runs offline.** No network calls during the presentation; everything is
   precomputed into `tradio/data/`.
8. **Don't break the live desk.** Existing files keep working. New code goes in
   `tradio/` and `web/tradio.html` unless your role file says otherwise.

## Setup and what's already done

- **Done:** `fed_speeches.py` knows Bowman and Waller; `python fed_speeches.py --tradio`
  downloads both event speeches and their baseline speeches (ids in
  `fed_speeches.TRADIO_SPEAKERS`, all checked to exist). `tradio/build_baselines.py`
  builds both baselines with the same rubric/model as `stance_scorer.py`, scoring only
  paragraphs about the policy outlook (dry run found 289 Bowman and 196 Waller baseline
  paragraphs).
- **Not done yet:** running it. It needs `gemapi.txt` and internet access to
  federalreserve.gov. Whoever holds the Gemini key runs
  `python fed_speeches.py --tradio && python tradio/build_baselines.py`, then commits
  `stance_baselines.json` and the new `transcripts/speech_*.json` files.
- **Keys:** only the Gemini key matters for Tradio. Kalshi/Polymarket historical data is
  public. All keys live in gitignored `*.txt` files; never commit them.
- **Renamed:** all user-facing text says Tradio, the live-trading opt-in is
  `TRADIO_ALLOW_LIVE=1`, and the logo is `web/tradio-logo.svg` / `web/tradio-mark.svg`.

- **Role 1 is done:** `tradio/data/` has `timeline.json` + `evidence.json` for three events:
  `bowman20250623`, `waller20250717` and `warsh20260828` (Warsh, Jackson Hole, 2026-08-28,
  T0 14:00:00Z, Kalshi September 2026 decision contracts). All spot checks pass; quote
  times are confirmed to be the END of each 1-minute candle (see `tradio/data/NOTES.md`).
  Contract labels look like "Sep 2026: hike 25 bp". Warsh's stance baseline already exists.

- **Live + trading (added after Gate 3):** `tradio/live.py` feeds the LIVE tab (live Kalshi
  tape + the live desk's scored transcript; `--simulate` replays a past event offline;
  `archive` saves a session as a history event and rebuilds the playbook).
  `tradio/playbook.py` learns trading rules from every event and writes a leave-one-out
  `backtest.json` per event; `python live.py --fast --playbook --mode demo --venues kalshi`
  trades by those rules on Kalshi's demo exchange.

## Who owns what

| Role | Person | Owns |
|---|---|---|
| 1 Market data + replay timeline | (name) | `tradio/events.yaml`, `tradio/ingest_markets.py`, `tradio/build_timeline.py`, `tradio/data/` |
| 2 Reaction analytics | (name) | `tradio/reaction.py`, `tradio/placebo.py`, `tests/test_reaction.py` |
| 3 Surprise scoring + pitch | (name) | `fed_speeches.py` (add speakers only), `tradio/build_baselines.py`, `tradio/surprise.py`, `tradio/validation/`, `docs/pitch.md` |
| 4 Visuals (terminal UI) | (name) | `web/tradio.html`, `web/tradio.js`, `web/tradio.css`, `web/data/tradio/mock/` |

Touching someone else's file: ask them first. Shared files (`CLAUDE.md`,
`docs/team/*`) change only with the whole team's agreement.

## Data contracts (the only thing roles depend on each other for)

All files live in `tradio/data/<event_id>/`. Event ids: `bowman20250623`, `waller20250717`, `warsh20260828`.

`timeline.json` — written by role 1 (speech fields filled by role 3's `surprise.py`):
```json
{
  "event_id": "bowman20250623",
  "title": "Bowman, Prague, 2025-06-23",
  "speaker": "michelle_bowman",
  "t0": "2025-06-23T14:00:00Z",
  "window": {"start": "2025-06-23T13:00:00Z", "end": "2025-06-23T15:30:00Z"},
  "speech": {"source_url": "...", "paragraphs": [
    {"id": "p0", "text": "...", "stance": -0.5, "z": -2.4, "relevant": true, "key_sentence": "..."}
  ]},
  "markets": [
    {"id": "kalshi:KXFEDDECISION-25JUL-C25", "venue": "kalshi", "label": "July: cut 25 bp",
     "exposure": "direct", "indicative": false, "tick": 0.01}
  ],
  "quotes": [{"t": "2025-06-23T14:07:00Z", "market": "kalshi:KXFEDDECISION-25JUL-C25",
              "bid": 0.15, "ask": 0.16, "mid": 0.155, "imputed": false, "volume": 129898}],
  "trades": [{"t": "2025-06-23T14:08:43.494590Z", "market": "kalshi:KXFEDDECISION-25JUL-C25",
              "price": 0.16, "size": 100, "taker_side": "yes"}]
}
```
`quotes.t` = END of the 1-minute Kalshi candle (`end_period_ts`); role 1 verifies this.
Polymarket points go in `quotes` with `bid`/`ask` = null and the market marked `indicative: true`.

`analysis.json` — written by role 2:
```json
{"event_id": "bowman20250623", "markets": {
  "kalshi:KXFEDDECISION-25JUL-C25": {
    "liquidity": "active", "pre_mid": 0.145, "alarm_t": "2025-06-23T14:07:00Z",
    "confirmed_by_trade": "2025-06-23T14:08:43.494590Z", "move_ticks": 7,
    "t50_min": null, "t90_min": null, "status": "reacted", "reason": "...", "placebo_p": null}
}}
```
The values above show the shape only; real values come from running the code.
`status` is one of: `reacted`, `expected_no_reaction`, `cant_tell_illiquid`,
`candidate_delay`, `candidate_inconsistency`.

## Order of work (gates)

1. **Gate 1:** `timeline.json` + `analysis.json` exist for Bowman (rough is fine).
2. **Gate 2:** `web/tradio.html` replays Bowman end to end from those files.
3. **Gate 3:** Waller works too and the side-by-side contrast shows. Feature freeze.
Nothing else (more events, live mode, placebo, scatter plot) until Gate 3 passes.

## Git

Branch per person (`role1-data`, `role2-reaction`, `role3-surprise`, `role4-visuals`), small commits, pull
`main` often, merge through PRs. Never commit `tradio/data/` raw dumps over ~5 MB;
commit the compact `timeline.json` / `analysis.json`.
