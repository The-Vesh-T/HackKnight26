# Role 3 — Surprise scoring + terminal UI

Read `CLAUDE.md` first. You own: speaker additions in `fed_speeches.py`,
`shockwave/surprise.py`, `web/shockwave.html`, `web/shockwave.js`, `web/shockwave.css`.

**Your goal:** (a) score Bowman's and Waller's speeches against their own history,
reusing MarketPulse's `stance_scorer.py` method; (b) build the replay terminal page.

## Part A — Surprise (first 8 hours)

1. Add `"bowman": "michelle_bowman"` and `"waller": "christopher_waller"` to
   `SPEAKER_KEYS` in `fed_speeches.py`. Download each speaker's speeches from mid-2023
   up to (not including) the event date, plus the event speeches `bowman20250623a`
   and `waller20250717a`. Don't change behavior for existing speakers.
2. `shockwave/surprise.py`: build baselines for the two new speakers with the same
   rubric and model as `stance_scorer.py` (import its functions; don't fork the rubric).
   Baseline = only speeches **before** the event date. Score every paragraph of the
   event speech, compute z, mark the top-|z| paragraph and its key sentence, and write
   them into `timeline.json` → `speech.paragraphs`.
3. **Validation (must report, whatever the result):**
   - Roles 1 and 2 hand-label 30 paragraphs (Bowman + Waller mixed) on the −1…+1 scale
     *before* seeing model scores. Report Spearman and sign agreement (targets ≥ 0.6, ≥ 85%).
   - Score the same 30 twice; report how many are identical.
   - Prediction to test: Bowman's top paragraph z ≤ −2, Waller's top |z| < 1. If not,
     we show the real number and explain it.

## Part B — Terminal page (`web/shockwave.html`)

Static page in the existing site's style (plain HTML/CSS/JS, no build step). It loads
`web/data/shockwave/<event_id>/timeline.json` and `analysis.json` and replays them
with a client-side clock. Start with mock files that follow the `CLAUDE.md` schema.

Layout (1920×1080, dark, dense, monospace numbers):
- **Top bar:** event picker (Bowman / Waller), market clock in UTC and ET, play/pause,
  speed 1×/10×/30×, scrubber with T0 and alarm markers.
- **Left:** transcript. At T0 all paragraphs appear; each is tinted by z (blue dovish,
  amber hawkish, grey irrelevant). The top-surprise sentence is underlined and pinned.
  Below it, the speaker baseline strip: past paragraph stances as dots, the mean ± sd
  band, today's paragraph as a large dot with its z.
- **Center:** absorption chart. Midpoint line per market (Kalshi solid, Polymarket
  dashed + "indicative"), bid–ask band, pre-event noise band, T0 line, alarm ticks,
  trade dots sized by volume, 50%/90% markers.
- **Right top:** propagation network (speech node → market nodes; node pulses when its
  alarm time passes; color = status).
- **Right bottom:** market cards: status chip, liquidity class, reaction minute, move in
  ticks, reason line.
- **Footer, always visible:** "Historical replay · Polymarket series indicative ·
  not trading advice".

Demo beat to support: pause at T0+1 min with Bowman's sentence highlighted; press play
at 30×; the cut line climbs over ~50 seconds of wall time; then switch to Waller.

## Done when
- Bowman replays end to end from real files with correct alarm markers and statuses.
- Seeking to any time shows the right state within a second.
- Every number on screen can be traced to a row in role 1's files.

## Don't
- Don't animate markets reacting sentence by sentence (the text was released whole).
- Don't edit `web/index.html` or the PHP files; link to `shockwave.html` from the
  existing nav only if the team agrees.
