# Role 4 — Visuals (terminal UI)

Read `CLAUDE.md` first. You own: `web/tradio.html`, `web/tradio.js`,
`web/tradio.css`, `web/data/tradio/mock/`.

**Your goal:** the replay terminal judges remember. It reads files the others produce;
it never computes analytics itself and never calls an API.

Start immediately with mock files in `web/data/tradio/mock/<event_id>/` that follow the
`CLAUDE.md` schemas exactly (copy the examples, invent a few dozen quotes, label them
MOCK on screen). Switch to the real files in `web/data/tradio/<event_id>/` as soon as
role 1 and role 2 deliver them (Gate 1).

## The page

Static page in the existing site's style (plain HTML/CSS/JS, no build step). It loads
`web/data/tradio/<event_id>/timeline.json` and `analysis.json` and replays them
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

Also add the evidence drawer after Gate 2: clicking any number opens the raw rows
behind it from role 1's `evidence.json`.

## Done when
- Bowman replays end to end from real files with correct alarm markers and statuses.
- Seeking to any time shows the right state within a second.
- Every number on screen can be traced to a row in role 1's files.
- Waller can be switched in for the contrast.

## Don't
- Don't animate markets reacting sentence by sentence (the text was released whole).
- Don't compute or tweak numbers in the browser; if something looks wrong, tell role 1 or 2.
- Don't edit `web/index.html` or the PHP files; link to `tradio.html` from the
  existing nav only if the team agrees.
