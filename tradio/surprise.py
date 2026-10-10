"""
tradio/surprise.py
==================
Role 3: scores each event speech against that speaker's own baseline and writes the
result into timeline.json -> speech.paragraphs (the data contract in CLAUDE.md).

    python tradio/surprise.py                        # score every event (Gemini calls)
    python tradio/surprise.py --event bowman20250623
    python tradio/surprise.py --from-cache           # rebuild paragraphs from saved scores, no Gemini

Needs gemapi.txt (or GEMINI_API_KEY) unless --from-cache.

How it works:
  * Same rubric and model as stance_scorer.py (we import score_stances; no fork).
  * Same paragraph filter as tradio/build_baselines.py: only paragraphs about the
    policy outlook are scored, so the event is measured the way the baseline was.
    Other paragraphs keep stance/z = null and relevant = false.
  * z = (stance - baseline mean) / baseline stdev, with no macro adjustment (the
    Bowman/Waller baselines were built without one).
  * The top paragraph is the relevant one with the largest |z|. Its key sentence is the
    sentence whose own score is furthest from the baseline mean in the same direction.
  * Raw Gemini scores are saved to tradio/validation/scores/<event_id>.json, so the
    numbers on screen can be traced and rebuilt without new calls.
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import stance_scorer  # noqa: E402
from tradio import build_baselines  # noqa: E402
from tradio.common import DATA_DIR, WEB_DATA_DIR, load_events, read_json, write_json  # noqa: E402

SCORES_DIR = os.path.join(ROOT, "tradio", "validation", "scores")
MIN_SENTENCE_WORDS = 6
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


def is_relevant(text: str) -> bool:
    return (len(text.split()) >= stance_scorer.MIN_ANSWER_WORDS
            and bool(build_baselines.MONETARY.search(text)))


def sentences(text: str) -> list:
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if len(s.split()) >= MIN_SENTENCE_WORDS]


def score_event(event_id: str, cfg: dict) -> dict:
    """Gemini pass: scores relevant paragraphs, then the sentences of the top one."""
    speaker = cfg["speaker"]
    base = stance_scorer.load_store()[speaker]
    speech = build_baselines.ensure_downloaded(cfg["speech_id"])
    texts = [seg["text"] for seg in speech["segments"]]
    relevant = [i for i, t in enumerate(texts) if is_relevant(t)]
    print(f"{event_id}: scoring {len(relevant)} of {len(texts)} paragraphs ({speaker})")
    scored = dict(zip(relevant, stance_scorer.score_stances([texts[i] for i in relevant])))

    paragraphs = []
    for i, text in enumerate(texts):
        s = scored.get(i)
        paragraphs.append({"id": f"p{i}", "text": text, "relevant": s is not None,
                           "stance": s["stance"] if s else None,
                           "summary": s["summary"] if s else None})

    top = pick_top(paragraphs, base)
    key = []
    if top is not None:
        sents = sentences(paragraphs[top]["text"])
        if sents:
            key = [{"text": t, "stance": r["stance"]}
                   for t, r in zip(sents, stance_scorer.score_stances(sents))]
    return {"event_id": event_id, "speaker": speaker, "speech_id": cfg["speech_id"],
            "model": stance_scorer.GEMINI_MODEL,
            "baseline": {"n": base["n"], "mean": base["mean"], "stdev": base["stdev"],
                         "built_by": base.get("built_by", "stance_scorer.py --seed")},
            "paragraphs": paragraphs, "top_sentences": key}


def zscore(stance, base: dict):
    if stance is None:
        return None
    return round((stance - base["mean"]) / base["stdev"], 3)


def pick_top(paragraphs: list, base: dict):
    best, best_abs = None, -1.0
    for i, p in enumerate(paragraphs):
        z = zscore(p["stance"], base)
        if z is not None and abs(z) > best_abs:
            best, best_abs = i, abs(z)
    return best


def key_sentence(top_sentences: list, top_z: float, base: dict):
    """Sentence furthest from the baseline mean in the top paragraph's direction."""
    if not top_sentences:
        return None
    sign = 1 if top_z >= 0 else -1
    best = max(top_sentences, key=lambda s: sign * (s["stance"] - base["mean"]))
    return best["text"]


def build_paragraphs(scores: dict) -> dict:
    """Cached scores -> timeline.json speech fields."""
    base = scores["baseline"]
    top = pick_top(scores["paragraphs"], base)
    out = []
    for i, p in enumerate(scores["paragraphs"]):
        z = zscore(p["stance"], base)
        out.append({"id": p["id"], "text": p["text"], "stance": p["stance"], "z": z,
                    "relevant": p["relevant"], "summary": p["summary"], "top": i == top,
                    "key_sentence": key_sentence(scores["top_sentences"], z, base) if i == top else None})
    return {"paragraphs": out, "top_paragraph": out[top]["id"] if top is not None else None,
            "baseline": base, "model": scores["model"]}


def write_timeline(event_id: str, speech_fields: dict) -> None:
    for path in (os.path.join(DATA_DIR, event_id, "timeline.json"),
                 os.path.join(WEB_DATA_DIR, event_id, "timeline.json")):
        if not os.path.isfile(path):
            print(f"  skipped {path} (missing; run role 1's build_timeline.py first)")
            continue
        timeline = read_json(path)
        timeline["speech"].update(speech_fields)
        write_json(path, timeline)


def report(event_id: str, fields: dict) -> None:
    top = next((p for p in fields["paragraphs"] if p["top"]), None)
    rel = [p for p in fields["paragraphs"] if p["relevant"]]
    print(f"  {len(rel)} scored paragraphs; baseline mean {fields['baseline']['mean']:+.3f}, "
          f"sd {fields['baseline']['stdev']:.3f}")
    if top:
        print(f"  top {top['id']}: stance {top['stance']:+.2f}, z {top['z']:+.2f}")
        print(f"  key sentence: {top['key_sentence']}")


def main():
    parser = argparse.ArgumentParser(description="Score Tradio event speeches against speaker baselines")
    parser.add_argument("--event", action="append", help="event id (default: all)")
    parser.add_argument("--from-cache", action="store_true",
                        help="rebuild timeline paragraphs from tradio/validation/scores/, no Gemini")
    args = parser.parse_args()
    events = load_events()["events"]
    for event_id in args.event or sorted(events):
        cache = os.path.join(SCORES_DIR, f"{event_id}.json")
        if args.from_cache:
            if not os.path.isfile(cache):
                print(f"{event_id}: no cached scores at {cache}")
                continue
            scores = read_json(cache)
        else:
            scores = score_event(event_id, events[event_id])
            write_json(cache, scores)
        fields = build_paragraphs(scores)
        write_timeline(event_id, fields)
        report(event_id, fields)


if __name__ == "__main__":
    main()
