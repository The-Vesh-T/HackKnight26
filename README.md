# Tradio

**Built at HackKnight26** · repo: https://github.com/The-Vesh-T/HackKnight26

Tradio measures how prediction markets absorb surprising statements from Federal
Reserve officials. It scores each passage of a speech against **that speaker's own
history** (a hawk sounding dovish is news; a dove sounding dovish isn't), then replays
the real Kalshi and Polymarket tape around the speech to show which markets reacted,
how fast, how fully, and which were too thin to judge.

Hero replay: **Governor Bowman, June 23, 2025** — a known hawk said she would support a
July cut. Control: **Governor Waller, July 17, 2025** — a known dove saying the same.

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

## Setup

```bash
pip install -r requirements.txt
# Gemini key in gemapi.txt (gitignored) — only needed for speech scoring

python fed_speeches.py --tradio            # download Bowman/Waller event + baseline speeches
python tradio/build_baselines.py --dry-run # check paragraph counts, no Gemini calls
python tradio/build_baselines.py           # build both baselines into stance_baselines.json
```

Team roles, data formats and ground rules: [CLAUDE.md](CLAUDE.md) and `docs/team/`.

## Repo map

| Path | Role |
|---|---|
| `tradio/` | New: market data ingest, reaction analytics, baselines, replay data |
| `web/tradio.*` | New: the replay terminal |
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
