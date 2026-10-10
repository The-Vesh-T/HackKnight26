"""Tradio history: every Fed Board speech since Kalshi listed per-meeting decision markets,
measured the same way as the hero events, so the learner has hundreds of examples.

    python -m tradio.history speeches           # refresh the speech list from the Fed's feed
    python -m tradio.history pull               # Kalshi candles + trades per speech (internet,
                                                #   no keys; ~10-20 min; resumable)
    python -m tradio.history analyze            # offline: timeline + reaction.py per speech
    python -m tradio.history text               # download speech texts (federalreserve.gov)
    python -m tradio.history score              # optional: Gemini stance scores (gemapi.txt)
    python -m tradio.history embed              # optional: Gemini text embeddings (gemapi.txt)
    python -m tradio.history all                # pull + analyze + text (no Gemini)

Options: --since 2024-11-01 (default), --limit N (first N speeches, for a quick test).

Where things go (tradio/data/history/):
    speeches.json             the speech list (committed): id, T0 in UTC, speaker, role
    <speech_id>/raw/          raw Kalshi responses (gitignored, like role 1's raw/)
    <speech_id>/timeline.json the replay window, same contract as the hero events (gitignored:
                              ~100 KB each; rebuilt offline from raw/ by `analyze`)
    <speech_id>/analysis.json role 2's reaction.py output (committed)
    <speech_id>/speech.json   speech text (gitignored; re-downloadable)
    embeddings.json           one 256-number Gemini embedding per speech (policy paragraphs,
                              averaged), for the learner's text features (committed)
    <speech_id>/meta.json     speech facts for the learner: speaker, role, next meeting,
                              policy-paragraph count, stance scores if scored (committed)

The history events live in their own folder so the demo's replay tabs and the playbook
are unchanged; tradio/learner.py reads both.

Choices:
- T0 = the time on the Fed's own speech feed (federalreserve.gov/json/ne-speeches.json),
  which is US Eastern. It matches the hand-checked T0s of all three hero events. Speeches
  without a time on the feed are skipped (no T0 = no reaction time).
- Markets = the five KXFEDDECISION contracts for the NEXT FOMC decision after the speech
  (H26/H25/H0/C25/C26). That series starts with the late-2024 meetings; before that Kalshi
  used rate-level FED-* markets, which measure something else, so the default start is
  2024-11-01.
- Window = T0-60 .. T0+90 min, candles padded by an hour before (same as role 1).
- Overlaps are flagged, not dropped: another Board speech within 90 min, or a T0 at
  8:30/10:00 ET on a weekday (when US data releases land), so the learner can exclude them.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import DATA_DIR, iso, parse_ts, read_json, to_unix, write_json  # noqa: E402

HIST_DIR = os.path.join(DATA_DIR, "history")
SPEECHES = os.path.join(HIST_DIR, "speeches.json")
FEED = "https://www.federalreserve.gov/json/ne-speeches.json"
ET = ZoneInfo("America/New_York")
PRE_MIN, POST_MIN, PAD_MIN = 60, 90, 60
DEFAULT_SINCE = "2024-11-01"
LEGS = ["H26", "H25", "H0", "C25", "C26"]
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# FOMC decision days (second day of each meeting), federalreserve.gov/monetarypolicy/fomccalendars.htm.
# 2025-2026 as in placebo.py; 2024 added here.
FOMC_DECISIONS = [
    "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17",
    "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
    "2026-10-28", "2026-12-09",
]
ROLES = [("Vice Chair for Supervision", "vice_chair_supervision"), ("Vice Chair", "vice_chair"),
         ("Chairman", "chair"), ("Chair", "chair"), ("Governor", "governor")]
RELEASE_TIMES_ET = {(8, 30), (10, 0)}     # when most US data releases land


# ------------------------------------------------------------------ speech list

def parse_feed_entry(e: dict):
    """One ne-speeches.json entry -> speech dict, or None if it has no usable link."""
    link = e.get("l") or ""
    m = re.search(r"/newsevents/speech/([a-z]+\d{8}[a-z])\.htm", link)
    if not m:
        return None
    sid = m.group(1)
    d = (e.get("d") or "").strip()
    t0 = None
    try:
        local = datetime.strptime(d, "%m/%d/%Y %I:%M:%S %p")
        t0 = iso(local.replace(tzinfo=ET).astimezone(timezone.utc))
    except ValueError:
        pass                                      # date only: no release time on the feed
    who = (e.get("s") or "").strip()
    role = next((key for label, key in ROLES if who.startswith(label)), "other")
    name = who
    for label, _ in ROLES:
        if who.startswith(label):
            name = who[len(label):].strip()
            break
    words = [w for w in re.sub(r"[^A-Za-z\s-]", " ", name).split() if len(w) > 1]
    speaker = f"{words[0]}_{words[-1]}".lower() if len(words) >= 2 else sid.rstrip("abcdefgh")
    return {"id": sid, "date": sid[-9:-1], "t0": t0, "who": who, "role": role, "speaker": speaker,
            "title": (e.get("t") or "").strip(), "url": f"https://www.federalreserve.gov{link}"}


def refresh_speeches():
    from tradio.ingest_markets import HEADERS
    import requests
    r = requests.get(FEED, headers=HEADERS, timeout=30)
    r.raise_for_status()
    feed = json.loads(r.content.decode("utf-8-sig"))
    speeches = [s for s in (parse_feed_entry(e) for e in feed) if s]
    speeches.sort(key=lambda s: (s["t0"] or s["date"], s["id"]))
    write_json(SPEECHES, {"source": FEED, "fetched_at": iso(datetime.now(timezone.utc)),
                          "speeches": speeches})
    print(f"wrote {SPEECHES}: {len(speeches)} speeches, {sum(1 for s in speeches if s['t0'])} with a time")


def load_speeches(since=DEFAULT_SINCE, limit=None):
    data = read_json(SPEECHES)["speeches"]
    now = datetime.now(timezone.utc)
    # a window still open (T0+90 min in the future) would be cached as if final: skip it
    out = [s for s in data if s["t0"] and s["t0"][:10] >= since
           and parse_ts(s["t0"]) + timedelta(minutes=POST_MIN + 5) <= now]
    return out[:limit] if limit else out


# ------------------------------------------------------------------ markets

def next_decision(t0: str):
    """First FOMC decision day after the speech's date (None if beyond the table)."""
    day = parse_ts(t0).astimezone(ET).date()
    for d in FOMC_DECISIONS:
        dd = date.fromisoformat(d)
        if dd > day:
            return dd
    return None


def event_ticker(decision: date) -> str:
    return f"KXFEDDECISION-{decision.year % 100:02d}{MON[decision.month - 1]}"


def discover(ev_ticker: str) -> list:
    """Market tickers in a KXFEDDECISION event: historical list, then live, then the
    usual five legs."""
    from tradio import ingest_markets as im
    for url in (f"{im.KALSHI}/historical/markets", f"{im.KALSHI}/markets"):
        try:
            data = im.get_json(url, {"event_ticker": ev_ticker, "limit": 50})
        except im.NotFound:
            continue
        tickers = [m["ticker"] for m in data.get("markets", []) if m.get("ticker")]
        if tickers:
            return sorted(tickers)
    return [f"{ev_ticker}-{leg}" for leg in LEGS]


def flags_for(s: dict, all_speeches: list) -> list:
    t0 = to_unix(s["t0"])
    out = []
    local = parse_ts(s["t0"]).astimezone(ET)
    if local.weekday() < 5 and (local.hour, local.minute) in RELEASE_TIMES_ET:
        out.append(f"T0 at {local:%H:%M} ET, when US data releases usually land")
    for o in all_speeches:
        if o["id"] != s["id"] and o["t0"] and abs(to_unix(o["t0"]) - t0) < POST_MIN * 60:
            out.append(f"another Board speech within 90 min ({o['id']})")
    return out


# ------------------------------------------------------------------ pull (network)

def pull_one(s: dict, force=False) -> str:
    from tradio import ingest_markets as im
    d = os.path.join(HIST_DIR, s["id"], "raw")
    os.makedirs(d, exist_ok=True)
    decision = next_decision(s["t0"])
    if decision is None:
        return "no FOMC date after this speech in the table"
    ev = event_ticker(decision)
    mpath = os.path.join(d, "markets.json")
    if force or not os.path.isfile(mpath):
        write_json(mpath, {"event_ticker": ev, "tickers": discover(ev)})
    tickers = read_json(mpath)["tickers"]
    t0 = to_unix(s["t0"])
    start, end = t0 - PRE_MIN * 60, t0 + POST_MIN * 60
    got = 0
    for tk in tickers:
        cpath = os.path.join(d, f"kalshi_candles_{tk}.json")
        if force or not os.path.isfile(cpath):
            try:
                resp = im.kalshi_candles(tk, start - PAD_MIN * 60, end)
            except im.NotFound:
                resp = {"candlesticks": [], "_source": "not_found"}
            write_json(cpath, {"ticker": tk, "start_ts": start - PAD_MIN * 60, "end_ts": end,
                               "response": resp})
        n = len(read_json(cpath)["response"].get("candlesticks", []))
        tpath = os.path.join(d, f"kalshi_trades_{tk}.json")
        if n and (force or not os.path.isfile(tpath)):
            try:
                trades = im.kalshi_trades(tk, start, end)
            except im.NotFound:
                trades = []
            write_json(tpath, {"ticker": tk, "min_ts": start, "max_ts": end, "trades": trades})
        got += bool(n)
    return f"{ev}: {got}/{len(tickers)} contracts with candles"


def pull(speeches, force=False):
    for i, s in enumerate(speeches, 1):
        try:
            msg = pull_one(s, force)
        except Exception as exc:                  # keep going; rerun resumes
            msg = f"error: {exc}"
        print(f"[{i}/{len(speeches)}] {s['id']} {s['t0']}  {msg}", flush=True)


# ------------------------------------------------------------------ analyze (offline)

def build_timeline(s: dict):
    """Raw files -> timeline dict (role 1's converters, read-only), or None."""
    from tradio import build_timeline as bt
    d = os.path.join(HIST_DIR, s["id"], "raw")
    mpath = os.path.join(d, "markets.json")
    if not os.path.isfile(mpath):
        return None
    t0 = to_unix(s["t0"])
    start, end = t0 - PRE_MIN * 60, t0 + POST_MIN * 60
    markets, quotes, trades = [], [], []
    for tk in read_json(mpath)["tickers"]:
        cpath = os.path.join(d, f"kalshi_candles_{tk}.json")
        if not os.path.isfile(cpath):
            continue
        mid = f"kalshi:{tk}"
        q, _ = bt.kalshi_quotes(read_json(cpath), mid, start, end)
        if not q:
            continue
        tpath = os.path.join(d, f"kalshi_trades_{tk}.json")
        tr = bt.kalshi_trades(read_json(tpath), mid) if os.path.isfile(tpath) else []
        markets.append({"id": mid, "venue": "kalshi", "ticker": tk, "label": bt.kalshi_label(tk),
                        "exposure": "direct", "indicative": False, "tick": 0.01})
        quotes += q
        trades += tr
    if not markets:
        return None
    quotes.sort(key=lambda q: (q["t"], q["market"]))
    trades.sort(key=lambda t: (t["t"], t["market"]))
    return {"event_id": s["id"], "title": f"{s['who']}, {s['t0'][:10]}", "speaker": s["speaker"],
            "t0": s["t0"], "window": {"start": iso(parse_ts(s["t0"]) - timedelta(minutes=PRE_MIN)),
                                       "end": iso(parse_ts(s["t0"]) + timedelta(minutes=POST_MIN))},
            "speech": {"source_url": s["url"], "speech_id": s["id"], "paragraphs": []},
            "markets": markets, "quotes": quotes, "trades": trades}


def analyze(speeches, all_speeches):
    from tradio import reaction
    done = 0
    for s in speeches:
        tl = build_timeline(s)
        d = os.path.join(HIST_DIR, s["id"])
        if tl is None:
            continue
        write_json(os.path.join(d, "timeline.json"), tl)
        an = reaction.analyze(tl)
        write_json(os.path.join(d, "analysis.json"), an)
        meta_path = os.path.join(d, "meta.json")
        meta = read_json(meta_path) if os.path.isfile(meta_path) else {}
        decision = next_decision(s["t0"])
        meta.update({"speech_id": s["id"], "t0": s["t0"], "speaker": s["speaker"], "who": s["who"],
                     "role": s["role"], "title": s["title"], "url": s["url"],
                     "next_decision": decision.isoformat() if decision else None,
                     "days_to_decision": ((decision - parse_ts(s["t0"]).astimezone(ET).date()).days
                                          if decision else None),
                     "flags": flags_for(s, all_speeches)})
        write_json(meta_path, meta)
        done += 1
    print(f"analyzed {done} of {len(speeches)} speeches -> {HIST_DIR}")


# ------------------------------------------------------------------ text + stance

def speech_text(sid: str) -> dict:
    """Speech text, cached in the history folder (not transcripts/, so MarketPulse's
    replay catalog doesn't fill up with 200 extra speeches)."""
    path = os.path.join(HIST_DIR, sid, "speech.json")
    if not os.path.isfile(path):
        import fed_speeches
        write_json(path, fed_speeches.fetch_speech(sid))
    return read_json(path)


def text(speeches):
    """Download each speech text (federalreserve.gov) and count policy-outlook paragraphs
    with the same filter as the baselines. No Gemini needed."""
    from tradio import build_baselines
    for i, s in enumerate(speeches, 1):
        meta_path = os.path.join(HIST_DIR, s["id"], "meta.json")
        if not os.path.isfile(meta_path):
            continue
        meta = read_json(meta_path)
        if "policy_paragraphs" in meta:
            continue
        try:
            data = speech_text(s["id"])
        except Exception as exc:
            print(f"[{i}/{len(speeches)}] {s['id']}: text not downloaded ({exc})")
            continue
        segs = data.get("segments", [])
        meta["paragraphs"] = len(segs)
        meta["policy_paragraphs"] = len(build_baselines.policy_paragraphs(data))
        write_json(meta_path, meta)
        time.sleep(0.3)
    print("text step done")


def pooled_baseline(store: dict):
    """One Board-wide baseline (mean, stdev) pooled from every speaker baseline we have."""
    n = sum(b["n"] for b in store.values() if b.get("n"))
    if not n:
        return None
    mean = sum(b["mean"] * b["n"] for b in store.values() if b.get("n")) / n
    var = sum(b["n"] * (b["stdev"] ** 2 + (b["mean"] - mean) ** 2) for b in store.values() if b.get("n")) / n
    return {"n": n, "mean": mean, "stdev": var ** 0.5}


def score(speeches):
    """Gemini stance scores of the policy paragraphs (same rubric as stance_scorer.py).
    z uses the speaker's own baseline when we have one, else a Board-wide pooled one."""
    import stance_scorer
    from tradio import build_baselines
    store = stance_scorer.load_store()
    pool = pooled_baseline(store)
    for i, s in enumerate(speeches, 1):
        meta_path = os.path.join(HIST_DIR, s["id"], "meta.json")
        if not os.path.isfile(meta_path):
            continue
        meta = read_json(meta_path)
        if "stance" in meta:
            continue
        data = speech_text(s["id"])
        paras = build_baselines.policy_paragraphs(data)
        if not paras:
            meta["stance"] = {"n": 0}
            write_json(meta_path, meta)
            continue
        scores = [r["stance"] for r in stance_scorer.score_stances(paras)]
        base, base_kind = store.get(s["speaker"]), "speaker"
        if not base or not base.get("stdev"):
            base, base_kind = pool, "pooled"
        zs = [(x - base["mean"]) / base["stdev"] for x in scores] if base else []
        top = max(zs, key=abs) if zs else None
        meta["stance"] = {"n": len(scores), "scores": [round(x, 3) for x in scores],
                          "mean": round(sum(scores) / len(scores), 3),
                          "top_z": round(top, 3) if top is not None else None,
                          "baseline": base_kind, "model": stance_scorer.GEMINI_MODEL}
        write_json(meta_path, meta)
        print(f"[{i}/{len(speeches)}] {s['id']}: {len(scores)} paragraphs, top z {meta['stance']['top_z']}")


EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 256


def embed(speeches):
    """Gemini embedding of each speech: the policy-outlook paragraphs (or, if a speech has
    none, its first eight paragraphs), embedded one by one and averaged. Resumable."""
    import stance_scorer
    from google import genai
    from google.genai import types
    from tradio import build_baselines
    path = os.path.join(HIST_DIR, "embeddings.json")
    store = read_json(path) if os.path.isfile(path) else {}
    store.update({"model": EMBED_MODEL, "dim": EMBED_DIM, "task_type": "SEMANTIC_SIMILARITY"})
    store.setdefault("speeches", {})
    client = genai.Client(api_key=stance_scorer._read_gemini_key())
    cfg = types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=EMBED_DIM)
    for i, s in enumerate(speeches, 1):
        if s["id"] in store["speeches"]:
            continue
        try:
            data = speech_text(s["id"])
        except Exception as exc:
            print(f"[{i}/{len(speeches)}] {s['id']}: no text ({exc})")
            continue
        texts = build_baselines.policy_paragraphs(data) or [g["text"] for g in data.get("segments", [])[:8]]
        texts = [t[:2000] for t in texts if t.strip()]
        if not texts:
            continue
        vecs = []
        for start in range(0, len(texts), 50):
            for attempt in range(6):
                try:
                    res = client.models.embed_content(model=EMBED_MODEL, contents=texts[start:start + 50],
                                                      config=cfg)
                    vecs += [e.values for e in res.embeddings]
                    break
                except Exception as exc:
                    if attempt == 5:
                        raise
                    print(f"  embed retry ({str(exc)[:80]})")
                    time.sleep(5 * (attempt + 1))
        mean = [sum(col) / len(vecs) for col in zip(*vecs)]
        store["speeches"][s["id"]] = [round(x, 5) for x in mean]
        print(f"[{i}/{len(speeches)}] {s['id']}: {len(texts)} paragraphs embedded", flush=True)
        if i % 10 == 0:
            write_json(path, store)
    write_json(path, store)
    print(f"wrote {path}: {len(store['speeches'])} speeches")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=("speeches", "pull", "analyze", "text", "score", "embed", "all"))
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.step == "speeches":
        refresh_speeches()
        return
    speeches = load_speeches(a.since, a.limit)
    everyone = load_speeches("2000-01-01")
    print(f"{len(speeches)} timed speeches since {a.since}")
    if a.step in ("pull", "all"):
        pull(speeches, a.force)
    if a.step in ("analyze", "all"):
        analyze(speeches, everyone)
    if a.step in ("text", "all"):
        text(speeches)
    if a.step == "score":
        score(speeches)
    if a.step == "embed":
        embed(speeches)


if __name__ == "__main__":
    main()
