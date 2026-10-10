# Role 2 — Reaction analytics

Read `CLAUDE.md` first. You own: `tradio/reaction.py`, `tradio/placebo.py`,
`tests/test_reaction.py`. Input: role 1's `timeline.json`. Output: `analysis.json`.

**Your goal:** say, for every market, whether it reacted, how fast, how fully, and —
most importantly — never confuse an illiquid market with a slow one.

Until role 1's real file exists, build against a hand-made fixture in
`tests/fixtures/bowman_small.json` that follows the `CLAUDE.md` schema.

## Method (implement exactly; tune thresholds only with team agreement)

Window: T0−60 min to T0+90 min. Work on Kalshi markets. Polymarket (indicative) gets
a status and a final move but **no timing** (no `alarm_t`, `t50_min`, `t90_min`), per
CLAUDE.md rule 2. Its `status` stays one of the five contract values; the row carries
`"indicative": true` and its `reason` starts with "Indicative:". (Changed 2026-10-09: a
status suffix would break the CLAUDE.md contract role 4 matches on.)

1. **Liquidity class** from T0−60 to T0: trade count, minutes with a quote change,
   median spread in ticks, imputed minutes.
   - `active`: ≥ 10 trades and median spread ≤ 2 ticks
   - `thin`: 1–9 trades, or spread 3–5 ticks
   - `dark`: 0 trades and no quote change, or spread > 5 ticks
2. **Series:** per-minute quote midpoint (not last trade), carried forward when imputed.
3. **Reaction time (band exit, agreed 2026-10-09):** pre-event band = min…max of the
   midpoint over T0−60…T0. Alarm = first minute after T0 where the midpoint is ≥ 1 tick
   outside that band and stays outside for 2 straight minutes; `alarm_t` = the first of
   those minutes. If a trade at the new level happened inside that minute, set
   `confirmed_by_trade` to its timestamp.
   *Why it changed:* the original two-sided CUSUM on 1-minute midpoint **changes**
   (k = 0.5 tick, h = 2 ticks) never alarmed on real Bowman data. The July cut moved
   0.145 → 0.215 as single 1-tick steps with flat minutes between, and each flat minute
   drains the sum. The CUSUM result is still output as `cusum_alarm_t` for comparison.
4. **Absorption:** final move = median midpoint over T0+75…T0+90 minus pre-event
   midpoint (median of T0−10…T0). Only if |final move| ≥ 2 ticks: absorption(t) =
   move(t) / final move; report `t50_min`, `t90_min` (minutes after T0).
5. **Status:**
   - `reacted`: alarm and |move| ≥ 2 ticks (and placebo p < 0.05 once placebo exists)
   - `expected_no_reaction`: exposure weak/none, no alarm
   - `cant_tell_illiquid`: thin or dark, no alarm — never `candidate_delay`
   - `candidate_delay`: exposure direct/partial, active, no alarm in first 10 min, alarm later
   - `candidate_inconsistency`: within one Kalshi event, outcome mids sum outside
     0.97–1.03 for > 5 min; or the same outcome differs > 2 ticks across venues for > 5 min
   Each status gets a one-line `reason` the UI can show verbatim.
6. **Placebo (`tradio/placebo.py`, after Gate 3):** 200 random fake T0s at the same
   hour of day on days without Fed events; p = share with |move| ≥ observed. Needs
   role 1 to pull those extra windows.

## Tests (must pass before Gate 1)
- Synthetic frozen market (no trades, constant quotes) → `cant_tell_illiquid`, never
  `reacted` or `candidate_delay`.
- Synthetic step of +5 ticks at T0+3 in an active market → `reacted`, alarm within
  T0+3…T0+5.
- One-tick noise only → no alarm.

## Expected on real Bowman data (a check, not a target to force)
`-H0` and `-C25` should alarm near the 14:02 and 14:07 candles; `KXRATECUTCOUNT-25DEC31-T0`
had no trades for an hour and should come out `cant_tell_illiquid`. If your code
disagrees, investigate and report — don't tune until it agrees.

**Result (2026-10-09):** `-C25` and `-H0` both alarm at 14:07 and come out `reacted`;
`KXRATECUTCOUNT-25DEC31-T0` is `cant_tell_illiquid`. Reported difference: `-H0` at 14:02
only moved half a tick (0.85 → 0.845), still inside its pre-event range (0.845–0.855);
its first full-tick move outside the range was 0.835 at 14:07.

## Don't
- Don't compare reaction times across venues more finely than ±1 minute.
- Don't call anything an opportunity or a mispricing in output text.
