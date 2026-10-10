# Neural network for the playbook (handoff)

Read `CLAUDE.md` first. This extends `tradio/playbook.py` (PR #13). Owner: whoever picks
this up. Ask Devesh before changing `live.py` or the page.

## Goal

Replace or back up the hand-learned playbook rules with a small neural network that
predicts, for a Fed speech surprise and one Kalshi Fed-decision contract, **whether that
contract moves ≥ 2 cents in the surprise's direction within 30 minutes**. The desk then
trades when the model says yes with enough confidence.

## The honest catch

3 events (what we have) is far too few to train anything. Step 1 is getting ~50–150
events. If that isn't done, don't train a network; say so on stage instead.

## Steps

1. **More events** (role 1 + role 3 pipelines, nothing new to invent):
   - Candidates: every Fed governor/chair speech and press conference with a prepared
     text, 2024-01 to now (`fed_speeches.py` lists them; `fed_transcripts.py` for pressers).
   - Kalshi `KXFEDDECISION-<next meeting>` contracts have 1-minute history for that span
     (see `tradio/ingest_markets.py`; the historical endpoints need no keys).
   - For each event add an entry to `tradio/events.yaml` (T0 = release time UTC, window
     T0−60 to T0+90 min, the next meeting's 5 decision legs), then run
     `ingest_markets.py`, `build_timeline.py`, `tradio/surprise.py` (Gemini, costs quota),
     `tradio/reaction.py`. Speakers need a baseline (`tradio/build_baselines.py`).
   - Write a script that does this in bulk: `tradio/bulk_events.py`.
2. **Dataset** `tradio/model/dataset.csv`, one row per (event, contract), built only from
   files we pulled:
   - features known BEFORE trading: top |z| and sign, number of |z| ≥ 2 paragraphs,
     speaker (one-hot), regime (`playbook.regime`), contract kind, pre-T0 mid, spread,
     liquidity class and pre-T0 trade count (role 2's `liquidity_detail`), minutes to the
     meeting, ET hour.
   - target: mid at T0+30 min minus mid at T0+1 min, in the direction
     `playbook.side_for()` predicts, ≥ 2 ticks → 1 else 0. Also keep the raw move for P&L.
   - never use anything after T0+1 min as a feature (that's the leak to watch for).
3. **Models** `tradio/model/train.py` (scikit-learn, already easy to install):
   - baseline A: current playbook rules; baseline B: `LogisticRegression`;
   - network: `MLPClassifier(hidden_layer_sizes=(16, 8), alpha=1e-2, early_stopping=True)`
     with `StandardScaler`.
   - evaluation: **leave-one-event-out** (`GroupKFold` by event). Report accuracy, AUC,
     and backtest P&L (enter at T0+1 min ask/bid, exit T0+30 min bid/ask, Kalshi fee
     `0.07·p·(1−p)` both legs — same as `playbook.backtest`).
   - save `tradio/model/model.joblib` + `tradio/data/model_report.json`.
4. **Hook it up** only if the network beats both baselines out of sample:
   - `playbook.gate()` gets an optional model check (`p_move ≥ 0.6`), recorded in the
     skip reason like the other rules.
   - page: a line in the Playbook drawer with the model's out-of-sample numbers next to
     the rules'.
5. **Tests** `tests/test_model.py`: no feature uses data after T0+1 min; folds never share
   an event; the report numbers match a re-run.

## Pitch line (only if true)

"With N speeches, a small neural network trained on Tradio's own data predicts which
contracts will move better than our hand rules: AUC X vs Y, leave-one-speech-out." If it
doesn't win: "We tried a neural net; with N speeches it doesn't beat simple rules yet,
so the desk uses the rules. More recorded speeches is how that changes."

Demo money only (`--mode demo`). Not trading advice.
