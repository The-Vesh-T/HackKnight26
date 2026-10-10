# Tradio pitch (3 minutes)

Draft by role 3. Screen steps match the controls in `web/tradio.html`. Rehearse
them with role 4 before the demo. Start the page with `python tradio/serve.py` (it also powers the Live tab's buttons),
then open `http://localhost:8000/tradio.html#bowman20250623`. The replay speed defaults
to 30×, so one replay minute takes 2 seconds. Every number here comes from
`tradio/data/<event>/analysis.json`, `timeline.json`, or `tradio/validation/`.
If one of those files changes, update this script to match.

## The one-line story

The same words from two different Fed speakers move markets very differently. Tradio
measures two things: how surprising a speech was **against that speaker's own history**,
and whether the market actually moved. Those are not the same question. Waller shows why.

## Script

| Time | Screen | Say |
|---|---|---|
| 0:00–0:20 | Slide 1 | "On June 23, 2025, Michelle Bowman, the Fed's most hawkish governor, said she'd support a rate cut in July. Three weeks later Chris Waller, a known dove, said the same thing. One of those moved prediction markets and one didn't. Tradio shows you which, how fast, and whether you can trust the answer." |
| 0:20–0:40 | Bowman tab. Press **0** (or click **Jump to release**): paused at T0+1 min, 10:01 ET | "This is the real Kalshi and Polymarket tape around the release. The prepared text came out all at once at 10:00 a.m. Tradio scores every paragraph against Bowman's own 289-paragraph history." |
| 0:40–1:00 | Speech panel, top paragraph highlighted | "The most surprising paragraph scores z = −3.5, three and a half standard deviations more dovish than her usual. Key sentence: *'I think that we should put more weight on downside risks to our employment mandate going forward.'*" |
| 1:00–1:25 | Press **space** at 30×. About 12 s later, press **space** again at **10:07** | "By the 10:07 candle, both of Kalshi's July contracts trip the alarm. On the July-hold contract, a real trade at 10:06:30 inside that candle confirms it, so this isn't just a quote flicker." |
| 1:25–1:50 | Drag the scrubber to **10:42** (T0+42). For a live feel, use 60× and talk over the ~35 s it takes | "Half the move is done by minute 24 and 90% by minute 42. The chance of a July cut rose 8 points from 14.5%; the chance of a hold fell 8.5 points from 85%. The Polymarket lines moved the same way, but we label them indicative, because their history isn't reliable enough for timing." |
| 1:50–2:20 | Click the **Bowman vs Waller** tab (side by side; the clock keeps its position) | "Now Waller. Against his own history his speech is *also* a big surprise: z = −2.8. He opens by saying the FOMC should cut 25 basis points in July. But the market barely moved: the chance of a July hold dropped one point from 95.5%. That's because he had already said it on CNBC on June 20, so it was priced in. Surprise against a speaker's history isn't surprise against the market. That's why Tradio reads the tape and doesn't just score the text." |
| 2:20–2:35 | Waller tab, market rows marked "can't tell" | "And where a market was too thin to say anything, like Waller's July-cut contract, Tradio says *can't tell* instead of calling it slow." |
| 2:35–2:50 | Slide 3 | "Two speeches is a story, not proof, so we ran the same pipeline on 206 Fed speeches and loaded them into Snowflake. Across the 115 we can score, when the surprise is big, active Kalshi contracts react about three times as often: 30.5% versus 9.3%. Every result file is fingerprinted on Solana, so anyone can check we didn't edit them afterwards." |
| 2:50–3:00 | Slide 5 | Honest limits (below), then: "Read-only, no trading, runs offline. That's Tradio." |

## Slides

**1. The problem: same words, different speaker.** Bowman and Waller both back a
July 2025 cut. Kalshi July cut: +8 points after Bowman. July hold: −1 point after Waller.

**2. The evidence (n = 2, labeled as such).**

| | Bowman, Jun 23 | Waller, Jul 17 |
|---|---|---|
| Top paragraph z (vs own history) | −3.50 | −2.84 |
| Kalshi July cut | 14.5%, +8 points, reacted | 3.5%, thin, can't tell |
| Kalshi July hold | 85%, −8.5 points, reacted | 95.5%, −1 point, no reaction expected |
| Alarm (candle end) / trade confirm | 10:07 / 10:06:30 ET (hold contract) | n/a |
| Half / 90% of move | 21–24 min / 42–70 min | n/a |

**3. At scale: 206 speeches (observational, not causal).** Same pipeline, all 206
speeches loaded into Snowflake (`tradio/sql/snowflake_insights.sql`). 115 of them have
a stance score and at least one Kalshi contract that was active before T0:

| Top paragraph \|z\| | Speeches | Active contracts | Reacted |
|---|---|---|---|
| ≥ 2 | 38 | 82 | 30.5% |
| 1 to 2 | 26 | 46 | 13.0% |
| < 1 | 51 | 118 | 9.3% |

By speaker: chair 32.4%, vice chair 20.0%, governor 10.1%.
Every result file (418 of them) is hashed into one root and anchored on Solana devnet;
`python -m tradio.receipts verify` rechecks it.

**4. How it works.**
1. Gemini scores each paragraph −1 (dovish) to +1 (hawkish) with Tradio's stance rubric.
2. z is computed against that speaker's own baseline, using only policy paragraphs.
3. Kalshi 1-minute candles and trades go on one timeline with the text release at T0.
4. Each market gets one of five statuses: reacted, expected no reaction, can't tell
   (illiquid), candidate delay, candidate inconsistency.

**5. Honest limits.**
- The replay is two events. The 206-speech table is observational: big surprises
  and big reactions go together, but other news on the same day could explain some of it.
- Before we tested it, we predicted that Waller's top |z| would be under 1. It
  came out at 2.84, so the prediction failed. That miss is the point of slide 2.
- Polymarket history is indicative only. We never use it for timing.
- Placebo test: not run yet. (Update this line if role 2 runs it.)
- Stance validation:
  - Rescoring the same 30 paragraphs gave 29 of 30 identical scores, with a
    largest change of 0.3.
  - Hand-label agreement: **TODO**. Fill in from `tradio/validation/metrics.json`
    once roles 1, 2 and 4 submit labels, whatever it shows.
- Warsh (Jackson Hole 2026, top z +3.05) is scored against a baseline of his press
  conference answers. Those were not filtered to policy paragraphs, so his z isn't
  comparable to the other two. Leave Warsh out of the main demo unless asked.

## Sources for the spoken claims
- Waller's June 20, 2025 CNBC remarks: [Bloomberg](https://www.bloomberg.com/news/articles/2025-06-20/waller-says-fed-could-cut-interest-rates-as-soon-as-july),
  [Axios](https://www.axios.com/2025/06/20/fed-s-waller-rate-cut-should-be-on-the-table-for-july-macro).
- Slide 3: `python -m tradio.warehouse snowflake-query` (same numbers from the
  local files via `python -m tradio.warehouse export`); receipt in `tradio/data/receipts/data.json`.
- All other numbers: `tradio/data/{bowman20250623,waller20250717}/analysis.json`,
  `tradio/validation/scores/`, `tradio/validation/model_scores.json`.
