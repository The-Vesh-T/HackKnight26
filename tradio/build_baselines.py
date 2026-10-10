"""
tradio/build_baselines.py
=========================
Builds Bowman's and Waller's stance baselines for the Tradio replays, using the
same Gemini rubric and model as stance_scorer.py, and saves them into
stance_baselines.json under "michelle_bowman" and "christopher_waller"
(same format as the existing Warsh / Powell / Trump baselines).

    python tradio/build_baselines.py              # download missing speeches + build both
    python tradio/build_baselines.py --dry-run    # download + show what would be scored, no Gemini calls
    python tradio/build_baselines.py --speaker michelle_bowman

Needs gemapi.txt (or GEMINI_API_KEY) and internet access to federalreserve.gov.

Rules (see CLAUDE.md):
  * Only speeches BEFORE the event date go in a baseline (lists live in
    fed_speeches.TRADIO_SPEAKERS).
  * Speeches mix monetary policy with banking supervision and thank-yous. Those
    would all score 0.0 ("not about policy") and shrink the spread, inflating z.
    So only paragraphs that mention the policy outlook (MONETARY regex below) are
    scored. The filter is deliberately simple and visible; change it only with the
    team's agreement, because it changes every z-score.
"""

import argparse
import json
import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fed_speeches  # noqa: E402
import stance_scorer  # noqa: E402

MONETARY = re.compile(
    r"\b(inflation|policy rate|federal funds|interest rate|rate cut|rate hike|cut(ting)? (the )?rate|"
    r"monetary policy|FOMC|Committee|labor market|employment|unemployment|price stability|"
    r"tariff|disinflation|restrictive|accommodative|neutral rate|balance sheet)\b",
    re.I,
)


def speech_path(speech_id: str) -> str:
    return os.path.join(fed_speeches.TRANSCRIPT_DIR, f"speech_{speech_id}.json")


def ensure_downloaded(speech_id: str) -> dict:
    path = speech_path(speech_id)
    if not os.path.isfile(path):
        data = fed_speeches.fetch_speech(speech_id)
        os.makedirs(fed_speeches.TRANSCRIPT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        print(f"  downloaded {speech_id}: {len(data['segments'])} paragraphs")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def policy_paragraphs(data: dict) -> list:
    out = []
    for seg in data["segments"]:
        text = seg["text"]
        if len(text.split()) >= stance_scorer.MIN_ANSWER_WORDS and MONETARY.search(text):
            out.append(text)
    return out


def build(speaker: str, dry_run: bool):
    cfg = fed_speeches.TRADIO_SPEAKERS[speaker]
    event_date = re.search(r"(\d{8})", cfg["event"]).group(1)
    print(f"\n{speaker}: baseline from {len(cfg['baseline'])} speeches before {event_date}")
    ensure_downloaded(cfg["event"])  # so role 3 has the event text too
    entries = []
    for sid in cfg["baseline"]:
        sdate = re.search(r"(\d{8})", sid).group(1)
        assert sdate < event_date, f"{sid} is not before the event {cfg['event']}"
        paras = policy_paragraphs(ensure_downloaded(sid))
        print(f"  {sid}: {len(paras)} policy paragraphs")
        if dry_run or not paras:
            continue
        for text, scored in zip(paras, stance_scorer.score_stances(paras)):
            entries.append({"date": sdate, "speech": sid, "stance": scored["stance"],
                            "summary": scored["summary"], "text": text[:300]})
    if dry_run:
        print("  (dry run: nothing scored or saved)")
        return
    stances = [e["stance"] for e in entries]
    store = stance_scorer.load_store()
    store[speaker] = {
        "dates": [f"speech_{sid}" for sid in cfg["baseline"]],
        "n": len(stances),
        "mean": statistics.mean(stances),
        "stdev": max(statistics.pstdev(stances), 0.05),  # same floor as stance_scorer.seed
        "entries": entries,
        "built_by": "tradio/build_baselines.py",
        "model": stance_scorer.GEMINI_MODEL,
        "event": cfg["event"],
    }
    stance_scorer.save_store(store)
    b = store[speaker]
    print(f"  saved: n={b['n']}  mean={b['mean']:+.3f}  stdev={b['stdev']:.3f}")


def main():
    parser = argparse.ArgumentParser(description="Build Tradio speaker baselines (Bowman, Waller)")
    parser.add_argument("--speaker", choices=sorted(fed_speeches.TRADIO_SPEAKERS), action="append")
    parser.add_argument("--dry-run", action="store_true", help="Download and count paragraphs only")
    args = parser.parse_args()
    for speaker in args.speaker or sorted(fed_speeches.TRADIO_SPEAKERS):
        build(speaker, args.dry_run)
    fed_speeches.build_catalog()


if __name__ == "__main__":
    main()
