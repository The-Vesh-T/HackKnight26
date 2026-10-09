# HackKnight26 — Shockwave (built on MarketPulse)

Read this whole file before doing anything. Then ask the user which role they are
(1, 2 or 3) and read their file in `docs/team/`. Only work in the files your role owns.

## What we are building this weekend

MarketPulse (everything already in this repo) listens to Fed speakers and scores each
passage's stance against that speaker's own history (`stance_scorer.py`, z-scores).

**Shockwave adds the other half: what the markets did next.** For a historical Fed
speech, we replay the speech release and the real Kalshi/Polymarket tape side by side,
measure how fast and how fully each related market absorbed the surprise, and refuse to
call a thin market "slow".

Hero demo (build this end to end before anything else):
- **Bowman, 2025-06-23.** Prepared text released 14:00:00 UTC (10:00 a.m. EDT). She was
  the Fed's most hawkish governor and said she would support a July cut. Kalshi July
  25 bp cut went 0.15 → 0.22 and July hold 0.86 → 0.77 over ~30–45 minutes.
- **Control: Waller, 2025-07-17.** Text released 22:30:00 UTC (6:30 p.m. EDT). A known
  dove being dovish. Same contracts moved only 1–2 cents.

Full evidence, algorithms and success criteria: the "Shockwave — Evidence & Build Plan"
doc (ask Devesh for the link). This file is the short version.

## Ground rules

1. **Never invent results.** Every number shown in the UI must come from a cached data
   file we pulled. If something is unverified, label it on screen.
2. **Polymarket price history is "indicative" only.** The same contract returned
   contradictory values depending on the query window. Never use it for timing claims.
3. **Speech text arrives all at once at T0.** Bowman and Waller released prepared texts.
   Never animate the market reacting to sentences "as she spoke".
4. **No trading in Shockwave.** Do not import or modify `kalshi_trader.py`,
   `polymarket_client.py` order code, `manual_orders.py`, `trading_common.py`,
   `control_worker.py`. Shockwave is read-only market data.
5. **No keys in git.** Keys stay in the gitignored `*.txt` files. Kalshi/Polymarket
   historical endpoints we use need no auth.
6. **Times:** data files store UTC ISO 8601 (`2025-06-23T14:00:00Z`). The UI shows ET too.
7. **Demo runs offline.** No network calls during the presentation; everything is
   precomputed into `shockwave/data/`.
8. **Don't break MarketPulse.** Existing files keep working. New code goes in
   `shockwave/` and `web/shockwave.html` unless your role file says otherwise.

## Who owns what

| Role | Person | Owns |
|---|---|---|
| 1 Market data + replay timeline | (name) | `shockwave/events.yaml`, `shockwave/ingest_markets.py`, `shockwave/build_timeline.py`, `shockwave/data/` |
| 2 Reaction analytics | (name) | `shockwave/reaction.py`, `shockwave/placebo.py`, `tests/test_reaction.py` |
| 3 Surprise scoring + terminal UI | (name) | `fed_speeches.py` (add speakers only), `shockwave/surprise.py`, `web/shockwave.html`, `web/shockwave.js`, `web/shockwave.css` |

Touching someone else's file: ask them first. Shared files (`CLAUDE.md`,
`docs/team/*`) change only with the whole team's agreement.

## Data contracts (the only thing roles depend on each other for)

All files live in `shockwave/data/<event_id>/`. Event ids: `bowman20250623`, `waller20250717`.

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
2. **Gate 2:** `web/shockwave.html` replays Bowman end to end from those files.
3. **Gate 3:** Waller works too and the side-by-side contrast shows. Feature freeze.
Nothing else (more events, live mode, placebo, scatter plot) until Gate 3 passes.

## Git

Branch per person (`role1-data`, `role2-reaction`, `role3-ui`), small commits, pull
`main` often, merge through PRs. Never commit `shockwave/data/` raw dumps over ~5 MB;
commit the compact `timeline.json` / `analysis.json`.
