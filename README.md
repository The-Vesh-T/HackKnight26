# Tradio

**Built at HackKnight26** · repo: https://github.com/The-Vesh-T/HackKnight26

Tradio measures how prediction markets absorb surprising statements from Federal
Reserve officials. It scores each passage of a speech against **that speaker's own
history** (a hawk sounding dovish is news; a dove sounding dovish isn't), then replays
the real Kalshi and Polymarket tape around the speech to show which markets reacted,
how fast, how fully, and which were too thin to judge.

Replay events (all with real, cached Kalshi + Polymarket data):

| Event | What happened | Why it's in the demo |
|---|---|---|
| **Chair Warsh, Jackson Hole, Aug 28, 2026** | Hawkish speech; Kalshi's September "hike 25 bp" went from 0.29/0.30 to 0.51/0.52 within ~35 minutes, then pulled back | Newest and biggest reaction |
| **Governor Bowman, Jun 23, 2025** | A known hawk said she would support a July cut; July "cut 25 bp" 0.15 → 0.22 over ~30 minutes | Surprise relative to the speaker |
| **Governor Waller, Jul 17, 2025** | A known dove saying the same thing; the same contracts moved 1–2 cents | Control: expected, so little reaction |

Tradio is built on our earlier project, MarketPulse (live Fed-speech stance scoring and
prediction-market tooling), which we reuse here with permission. New this weekend: the
historical market-reaction analysis and the replay terminal (`tradio/`, `web/tradio.*`).

> Status: in progress during HackKnight26. Numbers in the replay come only from cached
> Kalshi/Polymarket data; Polymarket series are labeled indicative.

## How it works

```
 Fed speech text (released at T0)
   │  Gemini scores each paragraph −1 dovish … +1 hawkish
   ▼
 z = (stance − speaker mean) / speaker sd     ← baseline from that speaker's earlier speeches
   │
   ▼
 Kalshi + Polymarket historical trades and 1-minute quotes around T0
   │  liquidity class · change detection · absorption curve · placebo test
   ▼
 replay terminal: transcript, speaker baseline, price chart, propagation network
```

## Live mode

The same screen works during a real Fed speech. The **● LIVE** tab on `web/tradio.html`
shows the transcript as it's spoken, scored against the speaker's own history by
MarketPulse's live listener, next to Kalshi's live Fed-decision prices with the same
reaction checks. Until the 90-minute window closes it only says "alarm · moving" or
"watching"; the final verdict appears when the window is complete. Afterwards one command
keeps the session as a History tab.

```bash
# terminal 1+2: transcribe and score the speech (MarketPulse; dry, no watchlist => never trades)
python speechtxt.py --url https://www.federalreserve.gov/live-broadcast.htm
python live.py --speaker christopher_waller --no-prewarm --mode dry
# terminal 3: live Kalshi tape + reaction checks -> web/data/tradio/live/
python tradio/live.py --speaker christopher_waller --t0 now
# terminal 4: the page
cd web && python3 -m http.server 8000     # open http://localhost:8000/tradio.html#live
# afterwards: keep it as a history event
python tradio/live.py archive waller20261015 --title "Waller, 2026-10-15"

# no speech on? rehearse offline: replay a past event through the live tab (labelled SIMULATED)
python tradio/live.py --simulate bowman20250623 --speed 30
```

`tradio/live.py` only reads MarketPulse's `live_state.json`; it never imports the
trading code. Kalshi's public endpoints need no keys.

## Team roles

Work is split into four roles. Each has a file in `docs/team/` that tells its owner (and
their Claude) exactly what to build, which files they own, and when they're done.
Ground rules and the shared data formats are in [CLAUDE.md](CLAUDE.md).

| Role | Owns | Status |
|---|---|---|
| **1 · Market data** — [role1-market-data.md](docs/team/role1-market-data.md) | Pulls and caches Kalshi/Polymarket trades and quotes, builds `timeline.json` and `evidence.json` per event | Done: 3 events, all spot checks pass |
| **2 · Reaction analytics** — [role2-reaction-analytics.md](docs/team/role2-reaction-analytics.md) | Detects which markets reacted, how fast and how fully, and which were too thin to judge; writes `analysis.json` | In progress |
| **3 · Surprise scoring + pitch** — [role3-surprise.md](docs/team/role3-surprise.md) | Scores each speech against the speaker's own history (Gemini), validates the scores, writes the demo script and slides | In progress |
| **4 · Visuals** — [role4-visuals.md](docs/team/role4-visuals.md) | The replay terminal page (`web/tradio.*`) | Starting later |

Order of work: Gate 1 = `timeline.json` + `analysis.json` for one event; Gate 2 = the
page replays it end to end; Gate 3 = all events and the contrast work. Nothing extra
until Gate 3.

## Setup

```bash
pip install -r requirements.txt

# Role 1 · market data (no keys needed; already run, results committed in tradio/data/)
python tradio/ingest_markets.py            # cache raw Kalshi/Polymarket data for every event
python tradio/build_timeline.py            # build timeline.json + tradio/data/NOTES.md (spot checks)
python tradio/build_evidence.py            # build evidence.json for the evidence drawer

# Role 3 · speech scoring (needs gemapi.txt, gitignored)
python fed_speeches.py --tradio            # download Bowman/Waller event + baseline speeches
python tradio/build_baselines.py --dry-run # check paragraph counts, no Gemini calls
python tradio/build_baselines.py           # build both baselines into stance_baselines.json
# Warsh's baseline (June/July 2026 press conferences) is already in stance_baselines.json.

python -m unittest tests.test_timeline tests.test_ingest tests.test_reaction tests.test_live
```

## Repo map

| Path | Role |
|---|---|
| `tradio/events.yaml` | The replay events, their markets, and hand-checked spot values |
| `tradio/ingest_markets.py`, `build_timeline.py`, `build_evidence.py` | Role 1: market data pipeline |
| `tradio/data/<event>/` | Cached raw data, `timeline.json`, `evidence.json` (and `analysis.json` from role 2) |
| `tradio/build_baselines.py` | Role 3: Bowman/Waller speaker baselines |
| `tradio/live.py` | Live mode: live Kalshi tape + MarketPulse's scored transcript into the LIVE tab; `archive` saves a session as a history event |
| `web/tradio.*` | New: the replay terminal (History tabs + LIVE tab) |
| `stance_scorer.py`, `fed_speeches.py`, `fed_transcripts.py` | Stance rubric, speaker baselines, speech and press-conference downloads |
| `live.py`, `speechtxt.py`, `mic.py`, `dashboard.py` | MarketPulse live desk (real-time transcription and scoring) |
| `market_router.py`, `kalshi_*.py`, `polymarket_client.py`, `run_*.py` | Market search and recommendations (live) |
| `kalshi_trader.py`, `manual_orders.py`, `trading_common.py`, `control_worker.py` | Order execution and hosted controls (not used by the Tradio replay) |
| `fred_client.py`, `social_sentiment.py`, `solana_proof.py`, `tiger_store.py`, `backboard_client.py` | Macro context, crowd sentiment, receipts, storage, memory |
| `transcripts/`, `stance_baselines.json` | Speech/press-conference transcripts and speaker baselines |

## Safety

The Tradio replay is read-only: it never places orders. The inherited live trading
path stays off by default and needs an approved operator, typing `LIVE`, and the local
`TRADIO_ALLOW_LIVE=1` opt-in (details in [docs/HOSTED_CONTROLS.md](docs/HOSTED_CONTROLS.md)).

## Team

(add names)

Not financial advice.
