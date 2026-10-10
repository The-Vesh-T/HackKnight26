# Role 3 — Surprise scoring + pitch

Read `CLAUDE.md` first. You own: speaker additions in `fed_speeches.py`, `tradio/build_baselines.py`,
`tradio/surprise.py`, `tradio/validation/`, `docs/pitch.md`.

**Your goal:** (a) score Bowman's and Waller's speeches against their own history,
reusing the `stance_scorer.py` method, and prove the scores are trustworthy;
(b) own the story: demo script and slides.

## Part A — Surprise (first 8–12 hours)

1. Already done (check it): `fed_speeches.py` has Bowman and Waller, and
   `python fed_speeches.py --tradio` downloads the event speeches plus baseline speeches.
   `tradio/build_baselines.py` builds both baselines (policy paragraphs only). Make sure
   it has been run with the Gemini key and the results committed; if not, run it.
2. `tradio/surprise.py`: load the baselines from `stance_baselines.json` (same rubric
   and model as `stance_scorer.py`; import its functions, don't fork the rubric). Score every paragraph of the
   event speech, compute z, mark the top-|z| paragraph and its key sentence, and write
   them into `timeline.json` → `speech.paragraphs`.
3. **Validation (must report, whatever the result):**
   - Roles 1, 2 and 4 hand-label 30 paragraphs (Bowman + Waller mixed) on the −1…+1 scale
     *before* seeing model scores. Report Spearman and sign agreement (targets ≥ 0.6, ≥ 85%).
   - Score the same 30 twice; report how many are identical.
   - Save labels, model scores and the metrics in `tradio/validation/` so the UI can
     show them.
   - Prediction to test: Bowman's top paragraph z ≤ −2, Waller's top |z| < 1. If not,
     we show the real number and explain it.

## Part B — Pitch (after Gate 1)

1. `docs/pitch.md`: the 3-minute demo script, beat by beat, with the exact replay
   times to pause at (agree them with role 4).
2. Four slides: the problem (same words, different speaker), the evidence (Bowman vs
   Waller, labeled n = 2), how it works, honest limits (Polymarket indicative, small
   sample, what the placebo test showed or that it wasn't run).
3. Rehearse the demo with role 4 at least three times after Gate 3.
4. Stretch, only after Gate 3: score 15–20 more speeches for the surprise-vs-move
   scatter, with role 1 pulling the market data.

## Done when
- Both events' `timeline.json` have scored paragraphs with z and a top sentence.
- Validation metrics are written down, whatever they show.
- The pitch script matches what the page actually does.

## Don't
- Don't change the rubric or model for existing speakers.
- Don't claim a statistical result in the pitch that we haven't computed.
