"""Tradio learner: predicts, before T0, which Kalshi Fed-decision contracts will react to a
speech, and keeps learning from every new event.

    python -m tradio.learner                    # rebuild tradio/data/model.json (+ web copy)
    python -m tradio.learner predict <event_id> # P(react) per contract from pre-T0 facts only

What it learns from: every event folder with timeline/analysis (the hero replays and
archived live sessions, tradio/data/<id>/) plus every backfilled Board speech
(tradio/data/history/<id>/, see history.py). One row per (speech, next-meeting
KXFEDDECISION contract).

Label: 1 if reaction.py says `reacted` or `candidate_delay`, 0 if `expected_no_reaction` /
`candidate_inconsistency`. Only contracts that were ACTIVE before T0 are labeled: in
reaction.py a thin market can only end `reacted` or `cant_tell_illiquid`, never "no
reaction", so keeping thin rows would let "thin" predict the label by construction.
Thin/dark contracts get no prediction ("not measurable"), same as on screen.

Features (known at T0, so the model can run live; market facts are strictly pre-T0, the
text features come from the text released AT T0):
  contract kind, pre-T0 price and its uncertainty p(1-p), pre-T0 trades / spread / quote
  changes / liquidity class, speaker role, days to the decision, after-hours release,
  overlap flags, share of the text about the policy outlook (if downloaded), and the
  stance surprise: |top z| and whether a surprise in that direction pushes this contract
  up (if scored).

How it is judged (prequential, "test then train"): events are taken in time order; each
event is predicted by models fitted only on events whose 90-minute window had already
CLOSED at its T0 (so two overlapping speeches never see each other's outcome), scored,
and only then added to the training set. That is exactly what happens live. Models:
  base_rate   share of past rows that reacted (the bar to beat)
  playbook    the playbook's idea: contract kinds that reacted before, in active markets
  logistic    L2 logistic regression on standardized features
  boosting    shallow gradient-boosted trees
  mlp         a small neural net (one hidden layer of 16, strong weight decay)
  mlp_bag     the same net trained 10 times with different random seeds, averaged
              (with ~30 reactions a single net is mostly seed noise; bagging cuts that)
  mlp_tuned   picks its own width and weight decay on the most recent quarter of ITS OWN
              training events (never the event being predicted), then a bag of 3
  (mlp_bag and mlp_tuned are in the full build, `python -m tradio.learner`; tests and
  build(extra_mlps=False) skip them because they cost ~20 extra fits per event)
The champion is the model with the lowest prequential log loss; it only counts as
"beats the base rate" if a bootstrap over clusters of overlapping speeches puts its whole
log-loss gain interval above zero, at 95% split across the four candidate models
(Bonferroni), because the champion is picked from four. With few events that honestly
says "not yet".
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
WINDOW_S = 90 * 60             # an event's label exists only once T0+90 min has passed
SEED = 0
BOOTSTRAP = 1000

FEATURES = (["kind_" + k for k in KINDS.values()]
            + ["pre_mid", "uncertainty", "log_trades", "spread_ticks", "quote_changes"]
            + ["role_" + r for r in ROLES] + ["is_testimony"]
            + ["log_days_to_decision", "after_hours", "overlap_speeches",
               "release_in_window", "release_day",
               "has_text", "policy_share", "has_stance", "abs_z", "aligned_z",
               "has_embedding", "novelty_vs_speaker", "novelty_vs_recent"])
EMB_PCA = 8                    # text models compress the speech embedding to this many components

# US data releases that land at 8:30 ET and move rate expectations on their own:
# BLS Employment Situation and CPI, from bls.gov/bls/news-release/empsit.htm and cpi.htm
# (read 2026-10-10; the Oct 2025 CPI was not published during the shutdown). If one lands
# inside a speech's T0-60..T0+90 window, a move may be the data, not the speech.
RELEASE_DAYS = {
    "jobs": ["2024-11-01", "2024-12-06", "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04",
             "2025-05-02", "2025-06-06", "2025-07-03", "2025-08-01", "2025-09-05", "2025-11-20",
             "2025-12-16", "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08",
             "2026-06-05", "2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02"],
    "cpi": ["2024-11-13", "2024-12-11", "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10",
            "2025-05-13", "2025-06-11", "2025-07-15", "2025-08-12", "2025-09-11", "2025-10-24",
            "2025-12-18", "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12",
            "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11"],
}
RELEASE_ET = (8, 30)


# ------------------------------------------------------------------ dataset

def _read(path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


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


def release_features(t0: str):
    """(release_in_window, release_day) for a speech T0: was a jobs/CPI report published
    that day, and did its 8:30 ET release fall inside T0-60..T0+90 min?"""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    local = parse_ts(t0).astimezone(et)
    day = local.date().isoformat()
    if not any(day in days for days in RELEASE_DAYS.values()):
        return 0.0, 0.0
    rel = local.replace(hour=RELEASE_ET[0], minute=RELEASE_ET[1], second=0, microsecond=0)
    delta = (rel - local).total_seconds()
    return float(-60 * 60 <= delta <= WINDOW_S), 1.0


_EMB = {}


def embeddings():
    """speech_id -> unit vector (history.py embed step), or {} if not built yet."""
    path = os.path.join(HIST_DIR, "embeddings.json")
    key = (path, os.path.getmtime(path) if os.path.isfile(path) else None)
    if key not in _EMB:
        raw = _read(path) or {}
        out = {}
        for sid, v in (raw.get("speeches") or {}).items():
            n = math.sqrt(sum(a * a for a in v)) or 1.0
            out[sid] = [a / n for a in v]
        _EMB.clear()
        _EMB[key] = out
    return _EMB[key]


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _mean(vs):
    n = len(vs)
    out = [sum(col) / n for col in zip(*vs)]
    norm = math.sqrt(sum(a * a for a in out)) or 1.0
    return [a / norm for a in out]


def novelty(sid: str, t0: str, speaker: str, speeches: dict):
    """How different a speech's text is from (a) the same speaker's EARLIER speeches and
    (b) every Board speech in the 30 days before it. Uses only speeches before T0."""
    E = embeddings()
    v = E.get(sid)
    if v is None:
        return None
    t = to_unix(t0)
    prior = [s for s in speeches.values() if s.get("t0") and s["id"] in E and to_unix(s["t0"]) < t]
    own = [E[s["id"]] for s in prior if s.get("speaker") == speaker]
    recent = [E[s["id"]] for s in prior if t - to_unix(s["t0"]) <= 30 * 86400]
    nv_own = 1 - _cos(v, _mean(own)) if own else None
    nv_recent = 1 - _cos(v, _mean(recent)) if recent else None
    return nv_own, nv_recent, v


def event_rows(event_dir: str, speeches: dict, for_predict: bool = False):
    """One event folder -> (event dict, rows) or None. for_predict accepts a live,
    still-provisional analysis (only its pre-T0 facts are used)."""
    an = _read(os.path.join(event_dir, "analysis.json"))
    if not an:
        return None
    if an.get("provisional") and not for_predict:
        return None
    meta = _read(os.path.join(event_dir, "meta.json")) or {}
    tl = _read(os.path.join(event_dir, "timeline.json")) or {}
    t0 = meta.get("t0") or tl.get("t0")
    if not t0:
        return None
    sid = meta.get("speech_id") or (tl.get("speech") or {}).get("speech_id")
    if not meta and sid:
        # hero/live events: use the backfill's facts for the same speech, so a speech is
        # encoded the same way wherever it came from
        meta = _read(os.path.join(HIST_DIR, sid, "meta.json")) or {}
    info = speeches.get(sid, {})
    speaker = meta.get("speaker") or info.get("speaker") or tl.get("speaker")
    role = meta.get("role") or info.get("role")
    if not role and speaker:                  # live sessions: the speaker's latest Board role
        mine = sorted((s for s in speeches.values() if s.get("speaker") == speaker and s.get("t0")),
                      key=lambda s: s["t0"])
        role = mine[-1]["role"] if mine else None
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
    overlap = sum(1 for f in flags if f.startswith("another Board speech"))
    rel_win, rel_day = release_features(t0)
    nov = novelty(sid, t0, speaker, speeches) if sid else None
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
        sp = det.get("median_spread_ticks")
        x["spread_ticks"] = min(sp if sp is not None else 10.0, 10.0)
        x["quote_changes"] = det.get("quote_change_minutes") or 0
        if role in ROLES:
            x["role_" + role] = 1.0
        x["is_testimony"] = float((meta.get("kind") or info.get("kind")) == "testimony")
        x["log_days_to_decision"] = math.log1p(days) if days is not None else 0.0
        x["after_hours"] = float(after_hours)
        x["overlap_speeches"] = float(overlap)
        x["release_in_window"], x["release_day"] = rel_win, rel_day
        if nov is not None:
            x["has_embedding"] = 1.0
            x["novelty_vs_speaker"] = nov[0] if nov[0] is not None else 0.0
            x["novelty_vs_recent"] = nov[1] if nov[1] is not None else 0.0
        x["has_text"] = float(has_text)
        x["policy_share"] = policy_share
        if z is not None:
            x["has_stance"] = 1.0
            x["abs_z"] = abs(z)
            x["aligned_z"] = z * _hawkish_pushes_up(k, pre)
        st = r.get("status")
        label = 1 if st in POSITIVE else 0 if st in NEGATIVE else None
        if r.get("liquidity") != "active":
            label = None                      # not measurable either way (see docstring)
        rows.append({"market": mid, "kind": k, "status": st, "liquidity": r.get("liquidity"),
                     "move_ticks": r.get("move_ticks"), "label": label,
                     "x": [x[f] for f in FEATURES],
                     "e": nov[2] if nov is not None else None})
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
        return kind in self.kinds             # every labeled row is an active market

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


class MLPBag:
    """Average of several identical small nets that differ only in their random seed."""

    def __init__(self, name="mlp_bag", seeds=10, hidden=(16,), alpha=1.0):
        self.name, self.seeds, self.hidden, self.alpha = name, seeds, hidden, alpha

    text = False

    def _net(self, hidden, alpha, seed):
        from sklearn.neural_network import MLPClassifier
        from sklearn.pipeline import make_pipeline
        # L-BFGS: full-batch, the recommended solver for small datasets (and ~3x faster here)
        return make_pipeline(_prep(self.text), MLPClassifier(
            hidden_layer_sizes=hidden, alpha=alpha, solver="lbfgs", max_iter=500, random_state=seed))

    def _fit_bag(self, X, y, hidden, alpha, seeds):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return [self._net(hidden, alpha, SEED + s).fit(X, y) for s in range(seeds)]

    def fit(self, X, y, kinds=None):
        self.fallback = BaseRate().fit(X, y)
        self.nets = self._fit_bag(X, y, self.hidden, self.alpha, self.seeds) if len(set(y)) == 2 else []
        return self

    def predict(self, X, kinds=None):
        if not self.nets:
            return self.fallback.predict(X)
        cols = [n.predict_proba(X)[:, 1] for n in self.nets]
        ps = [sum(float(c[i]) for c in cols) / len(cols) for i in range(len(X))]
        return [min(max(p, 1e-4), 1 - 1e-4) for p in ps]


class MLPTuned(MLPBag):
    """Chooses width and weight decay on the latest quarter of its own training rows (rows
    arrive in time order), then refits a small bag on all training rows with that choice.
    The event being predicted is never part of the choice."""
    GRID = [((h,), a) for h in (8, 16) for a in (0.3, 1.0, 3.0, 10.0)]

    def __init__(self, name="mlp_tuned", seeds=3):
        super().__init__(name=name, seeds=seeds)
        self.chosen = None

    def fit(self, X, y, kinds=None):
        self.fallback = BaseRate().fit(X, y)
        self.nets = []
        if len(set(y)) < 2:
            return self
        cut = int(len(y) * 0.75)
        hidden, alpha = self.hidden, self.alpha
        if len(set(y[:cut])) == 2 and sum(y[cut:]) > 0:
            best = None
            for h, a in self.GRID:
                net = self._fit_bag(X[:cut], y[:cut], h, a, 1)[0]
                ll = sum(_logloss(float(p), t) for p, t in zip(net.predict_proba(X[cut:])[:, 1], y[cut:]))
                if best is None or ll < best[0]:
                    best = (ll, h, a)
            _, hidden, alpha = best
        self.chosen = {"hidden": list(hidden), "alpha": alpha}
        self.nets = self._fit_bag(X, y, hidden, alpha, self.seeds)
        return self


class MLPText(MLPBag):
    """A small bag of nets that also read the speech text: the Gemini embedding of the
    policy paragraphs, compressed to EMB_PCA components fitted on training rows only."""
    text = True

    def __init__(self, name="mlp_text", seeds=5):
        super().__init__(name=name, seeds=seeds, hidden=(16,), alpha=3.0)


class Ensemble:
    """Average of several members' probabilities (usually steadier than any one of them)."""

    def __init__(self, name, members):
        self.name, self.members = name, members

    def fit(self, X, y, kinds=None):
        for m in self.members:
            m.fit(X, y, kinds)
        return self

    def predict(self, X, kinds=None):
        cols = [m.predict(X, kinds) for m in self.members]
        return [sum(c[i] for c in cols) / len(cols) for i in range(len(X))]


def _prep(text: bool):
    """Column front-end: base features standardized; with text, the embedding columns
    are compressed by PCA (fitted inside the pipeline, so only on training rows)."""
    from sklearn.compose import ColumnTransformer
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler
    nb = len(FEATURES)
    parts = [("base", StandardScaler(), list(range(nb)))]
    if text:
        parts.append(("text", PCA(n_components=EMB_PCA, random_state=SEED), slice(nb, None)))
    return ColumnTransformer(parts, remainder="drop")


def emb_dim():
    E = embeddings()
    return len(next(iter(E.values()))) if E else 0


def make_models(extra_mlps=False, text=None):
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    text = emb_dim() > 0 if text is None else text

    def logistic(t=False):
        return lambda: make_pipeline(_prep(t), LogisticRegression(C=0.5, max_iter=2000))

    def boosting():
        return lambda: make_pipeline(_prep(False), GradientBoostingClassifier(
            n_estimators=100, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=SEED))

    def mlp():
        return lambda: make_pipeline(_prep(False), MLPClassifier(
            hidden_layer_sizes=(16,), alpha=1.0, max_iter=2000, random_state=SEED))

    models = [
        BaseRate(),
        PlaybookRule(),
        Sk("logistic", logistic()),
        Sk("boosting", boosting()),
        Sk("mlp", mlp()),
    ] + ([MLPBag(), MLPTuned()] if extra_mlps else [])
    if text:
        models += [Sk("logistic_text", logistic(True)), MLPText()]
    members = [Sk("logistic", logistic()), Sk("boosting", boosting()), Sk("mlp", mlp())]
    if text:
        members.append(Sk("logistic_text", logistic(True)))
    models.append(Ensemble("ensemble", members))
    return models


def _matrix(rows):
    """Feature matrix: base features, then the speech embedding (zeros if none)."""
    d = emb_dim()
    return [r["x"] + (r["e"] if r.get("e") is not None and len(r["e"]) == d else [0.0] * d)
            for r in rows]


# ------------------------------------------------------------------ evaluation

def _labeled(rows):
    return [r for r in rows if r["label"] is not None]


def _logloss(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _avg_precision(ps, ys):
    if not sum(ys) or sum(ys) == len(ys):
        return None
    from sklearn.metrics import average_precision_score
    return float(average_precision_score(ys, ps))


def prequential(data, models=None, extra_mlps=False):
    """Test-then-train over events in time order. Returns per-event scored predictions."""
    models = models or make_models(extra_mlps)
    scored = []
    for ev, rows in data:
        lab = _labeled(rows)
        if not lab:
            continue
        train = _closed_before(data, ev)
        X = _matrix([r for _, rs in train for r in _labeled(rs)])
        y = [r["label"] for _, rs in train for r in _labeled(rs)]
        kinds = [r["kind"] for _, rs in train for r in _labeled(rs)]
        n_events = sum(1 for _, rs in train if _labeled(rs))
        if n_events < MIN_TRAIN_EVENTS or len(set(y)) < 2:
            continue
        Xe, ke = _matrix(lab), [r["kind"] for r in lab]
        preds = {}
        for m in models:
            m.fit(X, y, kinds)
            preds[m.name] = m.predict(Xe, ke)
        scored.append({"event": ev["event"], "t0": ev["t0"], "trained_on": n_events,
                       "rows": [{"market": r["market"], "label": r["label"],
                                 **{n: round(preds[n][i], 4) for n in preds}}
                                for i, r in enumerate(lab)]})
    return scored


def _closed_before(data, ev):
    """Events whose label already existed at ev's T0 (their T0+90 min had passed)."""
    t = to_unix(ev["t0"])
    return [(e, r) for e, r in data if e["event"] != ev["event"] and to_unix(e["t0"]) + WINDOW_S <= t]


def clusters(scored):
    """Group scored events whose windows overlap (same contracts, same tape): they are
    one observation for the bootstrap, not two. Returns lists of indexes into scored."""
    groups, end = [], None
    for i in sorted(range(len(scored)), key=lambda j: to_unix(scored[j]["t0"])):
        t = to_unix(scored[i]["t0"])
        if groups and t < end:
            groups[-1].append(i)
            end = max(end, t + WINDOW_S)
        else:
            groups.append([i])
            end = t + WINDOW_S
    return groups


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
    # bootstrap over clusters of overlapping events: log-loss gain vs base rate
    # (positive = better). 95% split over the candidate models (Bonferroni).
    rnd = random.Random(SEED)
    groups = clusters(scored)
    per_group = [[(_logloss(r["base_rate"], r["label"]), {n: _logloss(r[n], r["label"]) for n in names})
                  for i in g for r in scored[i]["rows"]] for g in groups]
    others = [n for n in names if n != "base_rate"]
    alpha = 0.05 / max(len(others), 1)
    board["n_clusters"] = len(groups)
    board["ci_level"] = round(1 - alpha, 4)
    for n in others:
        gains = []
        for _ in range(BOOTSTRAP):
            pick = [per_group[rnd.randrange(len(per_group))] for _ in per_group]
            flat = [x for g in pick for x in g]
            gains.append(sum(b - m[n] for b, m in flat) / len(flat))
        gains.sort()
        lo = gains[int(alpha / 2 * BOOTSTRAP)]
        hi = gains[min(BOOTSTRAP - 1, int((1 - alpha / 2) * BOOTSTRAP))]
        board["models"][n]["gain_vs_base_ci"] = [round(lo, 4), round(hi, 4)]
        board["models"][n]["beats_base_rate"] = lo > 0
    return board


# ------------------------------------------------------------------ build / predict

def _fit_all(data, name):
    lab = [r for _, rows in data for r in _labeled(rows)]
    X, y, kinds = _matrix(lab), [r["label"] for r in lab], [r["kind"] for r in lab]
    model = next((m for m in make_models(extra_mlps=True) if m.name == name), BaseRate())
    return model.fit(X, y, kinds) if y else None


def logistic_weights(data):
    """Standardized logistic coefficients on all labeled rows (what the linear model leans on)."""
    m = _fit_all(data, "logistic")
    if m is None or m.model is None:
        return None
    lr = m.model[-1]
    return dict(sorted(((f, round(float(w), 3)) for f, w in zip(FEATURES, lr.coef_[0])),
                       key=lambda kv: -abs(kv[1])))


def build(extra_mlps=False):
    data = dataset()
    names = [m.name for m in make_models(extra_mlps)]
    scored = prequential(data, extra_mlps=extra_mlps)
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
                   f"(log-loss gain 95% CI {board['models'][champion]['gain_vs_base_ci']}).")
    else:
        verdict = (f"No model beats the base rate yet on {board['n_events']} events predicted in time "
                   f"order; best is {champion}. Each new event is a new test.")
    out = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "n_events": len(data), "n_history_events": n_hist, "n_rows": len(all_rows),
        "n_labeled": len(lab), "positives": sum(r["label"] for r in lab),
        "excluded_not_measurable": sum(1 for r in all_rows if r["label"] is None),
        "features": FEATURES, "min_train_events": MIN_TRAIN_EVENTS,
        "champion": champion, "champion_beats_base_rate": beats, "verdict": verdict,
        "scoreboard": board, "logistic_weights": logistic_weights(data),
        "events": [{"event": ev["event"], "t0": ev["t0"], "source": ev["source"], "role": ev["role"],
                    "z": ev["z"], "rows": len(rows),
                    "reacted": sum(1 for r in rows if r["label"] == 1),
                    "not_measurable": sum(1 for r in rows if r["label"] is None)} for ev, rows in data],
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
    train = _closed_before(data, ev)
    name = model_name
    if name is None:
        try:
            name = read_json(MODEL)["champion"]
        except (OSError, ValueError, KeyError):
            name = "base_rate"
    model = _fit_all(train, name) or BaseRate().fit([], [])
    ps = model.predict(_matrix(rows), [r["kind"] for r in rows])
    return {"event": ev["event"], "model": name, "trained_on_events": len(train),
            "markets": {r["market"]: {"p_react": round(p, 4) if r["liquidity"] == "active" else None,
                                      "kind": r["kind"], "liquidity": r["liquidity"],
                                      "note": None if r["liquidity"] == "active" else
                                      "not measurable before T0 (not active); no prediction"}
                        for r, p in zip(rows, ps)}}


_LIVE = {}


def live_odds(event_dir: str):
    """The champion's P(react) for a live (still provisional) event, from pre-T0 facts.
    The champion is refitted on every finished event at most once per model.json build,
    so calling this every few seconds is cheap. None if there is no model yet."""
    M = _read(MODEL)
    if not M or not M.get("champion"):
        return None
    if _LIVE.get("key") != M.get("built_at"):
        data = dataset()
        _LIVE.update(key=M.get("built_at"), name=M["champion"], n=len(data),
                     model=_fit_all(data, M["champion"]) or BaseRate().fit([], []))
    got = event_rows(event_dir, _speech_index(), for_predict=True)
    if not got:
        return None
    _, rows = got
    ps = _LIVE["model"].predict(_matrix(rows), [r["kind"] for r in rows])
    return {"model": _LIVE["name"], "trained_on_events": _LIVE["n"], "built_at": M.get("built_at"),
            "markets": {r["market"]: (round(p, 4) if r["liquidity"] == "active" else None)
                        for r, p in zip(rows, ps)}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", nargs="?", default="build", choices=("build", "predict"))
    ap.add_argument("event", nargs="?")
    ap.add_argument("--model")
    ap.add_argument("--fast", action="store_true", help="skip mlp_bag / mlp_tuned (about 5x faster)")
    a = ap.parse_args(argv)
    if a.step == "predict":
        d = a.event if a.event and os.path.isdir(a.event) else os.path.join(DATA_DIR, a.event or "live")
        res = predict(d, a.model)
        if not res:
            raise SystemExit(f"no usable analysis.json in {d}")
        print(f"{res['event']}: {res['model']} trained on {res['trained_on_events']} earlier events")
        for mid, r in res["markets"].items():
            p = f"P(react) {r['p_react']:.2f}" if r["p_react"] is not None else r["note"]
            print(f"  {mid:36s} {r['liquidity'] or '':7s} {p}")
        return
    out = build(extra_mlps=not a.fast)
    print(f"model.json: {out['n_events']} events ({out['n_history_events']} backfilled), "
          f"{out['n_labeled']} labeled rows, {out['positives']} reacted, "
          f"{out['excluded_not_measurable']} not-active rows left out")
    for n, m in out["scoreboard"].get("models", {}).items():
        print(f"  {n:10s} log loss {m['log_loss']:.4f}  brier {m['brier']:.4f}  "
              f"skill {m['brier_skill']}  gain CI {m.get('gain_vs_base_ci', '-')}")
    print(out["verdict"])


if __name__ == "__main__":
    main()
