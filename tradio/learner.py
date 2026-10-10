"""Tradio learner: predicts, before T0, which Kalshi Fed-decision contracts will react to a
speech, and keeps learning from every new event.

    python -m tradio.learner                    # rebuild tradio/data/model.json (+ web copy)
    python -m tradio.learner predict <event_id> # P(react) per contract from pre-T0 facts only

What it learns from: every event folder with timeline/analysis (the hero replays and
archived live sessions, tradio/data/<id>/) plus every backfilled Board speech
(tradio/data/history/<id>/, see history.py). One row per (speech, next-meeting
KXFEDDECISION contract).

Label: 1 if reaction.py says `reacted` or `candidate_delay`, 0 if `expected_no_reaction` /
`candidate_inconsistency`. Rows reaction.py calls `cant_tell_illiquid` are left out of
training and scoring: a quiet thin market is "unknown", not "no reaction".

Features (all known before T0, so the model can run live):
  contract kind, pre-T0 price and its uncertainty p(1-p), pre-T0 trades / spread / quote
  changes / liquidity class, speaker role, days to the decision, after-hours release,
  overlap flags, share of the text about the policy outlook (if downloaded), and the
  stance surprise: |top z| and whether a surprise in that direction pushes this contract
  up (if scored).

How it is judged (prequential, "test then train"): events are taken in time order; each
event is predicted by models fitted only on earlier events, scored, and only then added
to the training set. That is exactly what happens live. Models:
  base_rate   share of past rows that reacted (the bar to beat)
  playbook    the playbook's idea: contract kinds that reacted before, in active markets
  logistic    L2 logistic regression on standardized features
  boosting    shallow gradient-boosted trees
  mlp         a small neural net (one hidden layer of 16, strong weight decay)
The champion is the model with the lowest prequential log loss; it only counts as
"beats the base rate" if a bootstrap over events puts the whole 95% interval of its
log-loss gain above zero. With few events that honestly says "not yet".
"""

import argparse
import math
import os
import random
import sys
import warnings
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import DATA_DIR, WEB_DATA_DIR, parse_ts, read_json, to_unix, write_json  # noqa: E402

MODEL = os.path.join(DATA_DIR, "model.json")
WEB_MODEL = os.path.join(WEB_DATA_DIR, "model.json")
HIST_DIR = os.path.join(DATA_DIR, "history")
POSITIVE = ("reacted", "candidate_delay")
NEGATIVE = ("expected_no_reaction", "candidate_inconsistency")
KINDS = {"H0": "hold", "H25": "hike_small", "H26": "hike_big", "C25": "cut_small", "C26": "cut_big"}
ROLES = ("chair", "vice_chair", "vice_chair_supervision", "governor")
MIN_TRAIN_EVENTS = 10          # models start predicting once this many events are known
SEED = 0
BOOTSTRAP = 1000

FEATURES = (["kind_" + k for k in KINDS.values()]
            + ["pre_mid", "uncertainty", "log_trades", "spread_ticks", "quote_changes",
               "active", "thin"]
            + ["role_" + r for r in ROLES]
            + ["log_days_to_decision", "after_hours", "n_flags",
               "has_text", "policy_share", "has_stance", "abs_z", "aligned_z"])


# ------------------------------------------------------------------ dataset

def _speech_index():
    path = os.path.join(HIST_DIR, "speeches.json")
    try:
        return {s["id"]: s for s in read_json(path)["speeches"]}
    except (OSError, ValueError, KeyError):
        return {}


def _kind(ticker: str):
    parts = ticker.split("-")
    return KINDS.get(parts[-1]) if parts[0] == "KXFEDDECISION" else None


def _hawkish_pushes_up(kind: str, pre: dict) -> int:
    """+1 if a hawkish surprise should raise this contract's YES price, -1 if lower, using
    the playbook's regime rule for 'hold'."""
    from tradio import playbook
    reg = playbook.regime(pre)
    return 1 if playbook.side_for(kind, "HAWKISH", reg) == "yes" else -1


def _top_z(timeline: dict, meta: dict):
    paras = [p for p in (timeline.get("speech") or {}).get("paragraphs", [])
             if p.get("relevant") and p.get("z") is not None]
    if paras:
        return max(paras, key=lambda p: abs(p["z"]))["z"]
    st = (meta or {}).get("stance") or {}
    return st.get("top_z")


def _days_to_decision(t0: str, ticker: str, meta: dict):
    if meta and meta.get("days_to_decision") is not None:
        return meta["days_to_decision"]
    from tradio import history
    d = history.next_decision(t0)
    return (d - parse_ts(t0).astimezone(history.ET).date()).days if d else None


def event_rows(event_dir: str, speeches: dict, for_predict: bool = False):
    """One event folder -> (event dict, rows) or None. for_predict accepts a live,
    still-provisional analysis (only its pre-T0 facts are used)."""
    try:
        an = read_json(os.path.join(event_dir, "analysis.json"))
    except (OSError, ValueError):
        return None
    if an.get("provisional") and not for_predict:
        return None
    meta_path = os.path.join(event_dir, "meta.json")
    meta = read_json(meta_path) if os.path.isfile(meta_path) else {}
    tl_path = os.path.join(event_dir, "timeline.json")
    tl = read_json(tl_path) if os.path.isfile(tl_path) else {}
    t0 = meta.get("t0") or tl.get("t0")
    if not t0:
        return None
    sid = meta.get("speech_id") or (tl.get("speech") or {}).get("speech_id")
    info = speeches.get(sid, {})
    role = meta.get("role") or info.get("role")
    local = parse_ts(t0).astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    after_hours = int(local.weekday() >= 5 or not (9 * 60 + 30 <= local.hour * 60 + local.minute < 16 * 60))
    z = _top_z(tl, meta)
    pre = {}
    cands = []
    for mid, r in an.get("markets", {}).items():
        tk = mid.split(":", 1)[-1]
        k = _kind(tk)
        if not mid.startswith("kalshi:") or not k or r.get("indicative"):
            continue
        # direct exposure only: the next decision's contracts
        if tl and not any(m["id"] == mid and m.get("exposure") == "direct" for m in tl.get("markets", [])):
            continue
        pre[k] = r.get("pre_mid")
        cands.append((mid, tk, k, r))
    if not cands:
        return None
    flags = meta.get("flags", [])
    days = _days_to_decision(t0, cands[0][1], meta)
    has_text = int(meta.get("policy_paragraphs") is not None)
    policy_share = (meta["policy_paragraphs"] / meta["paragraphs"]
                    if has_text and meta.get("paragraphs") else 0.0)
    rows = []
    for mid, tk, k, r in cands:
        det = r.get("liquidity_detail") or {}
        p = r.get("pre_mid")
        x = {f: 0.0 for f in FEATURES}
        x["kind_" + k] = 1.0
        x["pre_mid"] = p if p is not None else 0.5
        x["uncertainty"] = (p * (1 - p)) if p is not None else 0.25
        x["log_trades"] = math.log1p(det.get("trades") or 0)
        x["spread_ticks"] = min(det.get("median_spread_ticks") or 10.0, 10.0)
        x["quote_changes"] = det.get("quote_change_minutes") or 0
        x["active"] = float(r.get("liquidity") == "active")
        x["thin"] = float(r.get("liquidity") == "thin")
        if role in ROLES:
            x["role_" + role] = 1.0
        x["log_days_to_decision"] = math.log1p(days) if days is not None else 0.0
        x["after_hours"] = float(after_hours)
        x["n_flags"] = float(len(flags))
        x["has_text"] = float(has_text)
        x["policy_share"] = policy_share
        if z is not None:
            x["has_stance"] = 1.0
            x["abs_z"] = abs(z)
            x["aligned_z"] = z * _hawkish_pushes_up(k, pre)
        st = r.get("status")
        label = 1 if st in POSITIVE else 0 if st in NEGATIVE else None
        rows.append({"market": mid, "kind": k, "status": st, "liquidity": r.get("liquidity"),
                     "move_ticks": r.get("move_ticks"), "label": label,
                     "x": [x[f] for f in FEATURES]})
    ev = {"event": an.get("event_id") or os.path.basename(event_dir), "speech_id": sid, "t0": t0,
          "role": role, "source": "history" if os.path.dirname(event_dir) == HIST_DIR else "event",
          "z": z}
    return ev, rows


def dataset():
    """All events in time order: [(event, rows)], hero/live events win over history
    duplicates of the same speech."""
    speeches = _speech_index()
    out, seen = [], set()
    dirs = []
    for base in (DATA_DIR, HIST_DIR):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if name in ("live", "history") or not os.path.isdir(d):
                continue
            dirs.append(d)
    for d in dirs:
        got = event_rows(d, speeches)
        if not got:
            continue
        ev, rows = got
        key = ev["speech_id"] or ev["event"]
        if key in seen:
            continue
        seen.add(key)
        out.append((ev, rows))
    out.sort(key=lambda e: (to_unix(e[0]["t0"]), e[0]["event"]))
    return out


# ------------------------------------------------------------------ models

class BaseRate:
    name = "base_rate"

    def fit(self, X, y, kinds=None):
        self.p = (sum(y) + 1) / (len(y) + 2)
        return self

    def predict(self, X, kinds=None):
        return [self.p] * len(X)


class PlaybookRule:
    """The playbook's idea as a probability: rows of a contract kind that reacted before,
    in an active market, get the past hit rate of such rows; the rest get theirs."""
    name = "playbook"

    def _fires(self, x, kind):
        return kind in self.kinds and x[FEATURES.index("active")] == 1.0

    def fit(self, X, y, kinds):
        self.kinds = {k for k, lab in zip(kinds, y) if lab == 1}
        f = [self._fires(x, k) for x, k in zip(X, kinds)]
        hit = [lab for lab, fi in zip(y, f) if fi]
        miss = [lab for lab, fi in zip(y, f) if not fi]
        self.p_on = (sum(hit) + 1) / (len(hit) + 2)
        self.p_off = (sum(miss) + 1) / (len(miss) + 2)
        return self

    def predict(self, X, kinds):
        return [self.p_on if self._fires(x, k) else self.p_off for x, k in zip(X, kinds)]


class Sk:
    """Wraps a scikit-learn classifier; falls back to the base rate if y has one class."""

    def __init__(self, name, make):
        self.name, self.make = name, make

    def fit(self, X, y, kinds=None):
        self.fallback = BaseRate().fit(X, y)
        self.model = None
        if len(set(y)) == 2:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.model = self.make().fit(X, y)
        return self

    def predict(self, X, kinds=None):
        if self.model is None:
            return self.fallback.predict(X)
        p = self.model.predict_proba(X)[:, 1]
        return [min(max(float(v), 1e-4), 1 - 1e-4) for v in p]


def make_models():
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return [
        BaseRate(),
        PlaybookRule(),
        Sk("logistic", lambda: make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))),
        Sk("boosting", lambda: GradientBoostingClassifier(n_estimators=100, max_depth=2,
                                                          learning_rate=0.05, subsample=0.8,
                                                          random_state=SEED)),
        Sk("mlp", lambda: make_pipeline(StandardScaler(), MLPClassifier(
            hidden_layer_sizes=(16,), alpha=1.0, max_iter=2000, random_state=SEED))),
    ]


# ------------------------------------------------------------------ evaluation

def _labeled(rows):
    return [r for r in rows if r["label"] is not None]


def _logloss(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _avg_precision(ps, ys):
    pos = sum(ys)
    if not pos:
        return None
    order = sorted(range(len(ps)), key=lambda i: -ps[i])
    hits, total = 0, 0.0
    for rank, i in enumerate(order, 1):
        if ys[i]:
            hits += 1
            total += hits / rank
    return total / pos


def prequential(data, models=None):
    """Test-then-train over events in time order. Returns per-event scored predictions."""
    models = models or make_models()
    X, y, kinds, n_events = [], [], [], 0
    scored = []
    for ev, rows in data:
        lab = _labeled(rows)
        if lab and n_events >= MIN_TRAIN_EVENTS and len(set(y)) == 2:
            Xe, ke = [r["x"] for r in lab], [r["kind"] for r in lab]
            preds = {}
            for m in models:
                m.fit(X, y, kinds)
                preds[m.name] = m.predict(Xe, ke)
            scored.append({"event": ev["event"], "t0": ev["t0"],
                           "rows": [{"market": r["market"], "label": r["label"],
                                     **{n: round(preds[n][i], 4) for n in preds}}
                                    for i, r in enumerate(lab)]})
        if lab:
            X += [r["x"] for r in lab]
            y += [r["label"] for r in lab]
            kinds += [r["kind"] for r in lab]
            n_events += 1
    return scored


def scoreboard(scored, names):
    rows = [r for e in scored for r in e["rows"]]
    if not rows:
        return {"n_events": 0, "n_rows": 0, "models": {}}
    ys = [r["label"] for r in rows]
    board = {"n_events": len(scored), "n_rows": len(rows), "positives": sum(ys), "models": {}}
    for n in names:
        ps = [r[n] for r in rows]
        ll = sum(_logloss(p, y) for p, y in zip(ps, ys)) / len(rows)
        br = sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(rows)
        board["models"][n] = {"log_loss": round(ll, 4), "brier": round(br, 4),
                              "avg_precision": (round(_avg_precision(ps, ys), 4)
                                                if sum(ys) else None)}
    base = board["models"]["base_rate"]["brier"]
    for n in names:
        m = board["models"][n]
        m["brier_skill"] = round(1 - m["brier"] / base, 4) if base else None
    # bootstrap over events: log-loss gain vs base rate (positive = better than base rate)
    rnd = random.Random(SEED)
    per_event = [[(_logloss(r["base_rate"], r["label"]), {n: _logloss(r[n], r["label"]) for n in names})
                  for r in e["rows"]] for e in scored]
    for n in names:
        if n == "base_rate":
            continue
        gains = []
        for _ in range(BOOTSTRAP):
            pick = [per_event[rnd.randrange(len(per_event))] for _ in per_event]
            flat = [x for e in pick for x in e]
            gains.append(sum(b - m[n] for b, m in flat) / len(flat))
        gains.sort()
        lo, hi = gains[int(0.025 * BOOTSTRAP)], gains[int(0.975 * BOOTSTRAP) - 1]
        board["models"][n]["gain_vs_base_ci95"] = [round(lo, 4), round(hi, 4)]
        board["models"][n]["beats_base_rate"] = lo > 0
    return board


# ------------------------------------------------------------------ build / predict

def _fit_all(data, name):
    X, y, kinds = [], [], []
    for _, rows in data:
        for r in _labeled(rows):
            X.append(r["x"]), y.append(r["label"]), kinds.append(r["kind"])
    model = next(m for m in make_models() if m.name == name)
    return model.fit(X, y, kinds) if y else None


def logistic_weights(data):
    """Standardized logistic coefficients on all labeled rows (what the linear model leans on)."""
    m = _fit_all(data, "logistic")
    if m is None or m.model is None:
        return None
    lr = m.model[-1]
    return dict(sorted(((f, round(float(w), 3)) for f, w in zip(FEATURES, lr.coef_[0])),
                       key=lambda kv: -abs(kv[1])))


def build():
    data = dataset()
    names = [m.name for m in make_models()]
    scored = prequential(data)
    board = scoreboard(scored, names)
    ranked = sorted(board["models"].items(), key=lambda kv: kv[1]["log_loss"]) if board["models"] else []
    champion = ranked[0][0] if ranked else "base_rate"
    beats = bool(ranked) and champion != "base_rate" and board["models"][champion].get("beats_base_rate")
    all_rows = [r for _, rows in data for r in rows]
    lab = [r for r in all_rows if r["label"] is not None]
    n_hist = sum(1 for ev, _ in data if ev["source"] == "history")
    if not scored:
        verdict = (f"Only {len(data)} events; models start predicting after {MIN_TRAIN_EVENTS}. "
                   "Run tradio/history.py to backfill Board speeches.")
    elif beats:
        verdict = (f"{champion} beats the base rate on {board['n_events']} events predicted in time order "
                   f"(log-loss gain 95% CI {board['models'][champion]['gain_vs_base_ci95']}).")
    else:
        verdict = (f"No model beats the base rate yet on {board['n_events']} events predicted in time "
                   f"order; best is {champion}. Each new event is a new test.")
    out = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "n_events": len(data), "n_history_events": n_hist, "n_rows": len(all_rows),
        "n_labeled": len(lab), "positives": sum(r["label"] for r in lab),
        "excluded_cant_tell": sum(1 for r in all_rows if r["label"] is None),
        "features": FEATURES, "min_train_events": MIN_TRAIN_EVENTS,
        "champion": champion, "champion_beats_base_rate": beats, "verdict": verdict,
        "scoreboard": board, "logistic_weights": logistic_weights(data),
        "events": [{"event": ev["event"], "t0": ev["t0"], "source": ev["source"], "role": ev["role"],
                    "z": ev["z"], "rows": len(rows),
                    "reacted": sum(1 for r in rows if r["label"] == 1),
                    "cant_tell": sum(1 for r in rows if r["label"] is None)} for ev, rows in data],
        "predictions": scored,
        "caveat": ("Predictions use only facts known before T0. Scores are out-of-sample: every event "
                   "was predicted before the model saw it. Not trading advice."),
    }
    for path in (MODEL, WEB_MODEL):
        write_json(path, out)
    return out


def predict(event_dir: str, model_name: str = None):
    """P(react) per contract for one event folder, from a model fitted on every OTHER event."""
    data = dataset()
    got = event_rows(event_dir, _speech_index(), for_predict=True)
    if not got:
        return None
    ev, rows = got
    train = [(e, r) for e, r in data if e["event"] != ev["event"]
             and to_unix(e["t0"]) < to_unix(ev["t0"])]
    name = model_name
    if name is None:
        try:
            name = read_json(MODEL)["champion"]
        except (OSError, ValueError, KeyError):
            name = "base_rate"
    model = _fit_all(train, name) or BaseRate().fit([], [])
    ps = model.predict([r["x"] for r in rows], [r["kind"] for r in rows])
    return {"event": ev["event"], "model": name, "trained_on_events": len(train),
            "markets": {r["market"]: {"p_react": round(p, 4), "kind": r["kind"],
                                      "liquidity": r["liquidity"]} for r, p in zip(rows, ps)}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", nargs="?", default="build", choices=("build", "predict"))
    ap.add_argument("event", nargs="?")
    ap.add_argument("--model")
    a = ap.parse_args(argv)
    if a.step == "predict":
        d = a.event if a.event and os.path.isdir(a.event) else os.path.join(DATA_DIR, a.event or "live")
        res = predict(d, a.model)
        if not res:
            raise SystemExit(f"no usable analysis.json in {d}")
        print(f"{res['event']}: {res['model']} trained on {res['trained_on_events']} earlier events")
        for mid, r in res["markets"].items():
            print(f"  {mid:36s} {r['liquidity'] or '':7s} P(react) {r['p_react']:.2f}")
        return
    out = build()
    print(f"model.json: {out['n_events']} events ({out['n_history_events']} backfilled), "
          f"{out['n_labeled']} labeled rows, {out['positives']} reacted, "
          f"{out['excluded_cant_tell']} can't-tell rows left out")
    for n, m in out["scoreboard"].get("models", {}).items():
        print(f"  {n:10s} log loss {m['log_loss']:.4f}  brier {m['brier']:.4f}  "
              f"skill {m['brier_skill']}  gain CI {m.get('gain_vs_base_ci95', '-')}")
    print(out["verdict"])


if __name__ == "__main__":
    main()
