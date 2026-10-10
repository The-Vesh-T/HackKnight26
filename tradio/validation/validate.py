"""
tradio/validation/validate.py
=============================
Role 3's check that the Gemini stance scores can be trusted (docs/team/role3-surprise.md).

    python tradio/validation/validate.py sample    # pick 30 paragraphs, write a blank label sheet
    python tradio/validation/validate.py rescore   # score the 30 twice with Gemini
    python tradio/validation/validate.py report    # compare with the hand labels, write metrics.json

Steps:
  1. `sample` picks 15 Bowman + 15 Waller event-speech paragraphs (the ones surprise.py
     scores), shuffles them with a fixed seed, and writes labels_blank.csv. Each
     labeler copies it to labels_<name>.csv and fills the `label` column (-1 ... +1)
     WITHOUT looking at model scores. Use the rubric in stance_scorer.py.
  2. `rescore` scores the same 30 twice and saves both runs to model_scores.json.
  3. `report` averages the human labels per paragraph and reports Spearman and sign
     agreement against the first model run (targets: >= 0.6 and >= 85%), plus how many
     of the 30 scored identically across the two runs. Whatever it shows goes in the pitch.
"""

import argparse
import csv
import glob
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import stance_scorer  # noqa: E402
from tradio import build_baselines, surprise  # noqa: E402
from tradio.common import load_events, read_json, write_json  # noqa: E402

SAMPLE_PATH = os.path.join(HERE, "sample.json")
BLANK_PATH = os.path.join(HERE, "labels_blank.csv")
MODEL_PATH = os.path.join(HERE, "model_scores.json")
METRICS_PATH = os.path.join(HERE, "metrics.json")
EVENTS = {"bowman20250623": 15, "waller20250717": 15}
SEED = 26
SIGN_BAND = 0.25   # |x| < 0.25 counts as neutral when comparing signs


def sample() -> list:
    events = load_events()["events"]
    rng = random.Random(SEED)
    items = []
    for event_id, k in EVENTS.items():
        speech = build_baselines.ensure_downloaded(events[event_id]["speech_id"])
        texts = [s["text"] for s in speech["segments"]]
        candidates = [i for i, t in enumerate(texts) if surprise.is_relevant(t)]
        for i in sorted(rng.sample(candidates, min(k, len(candidates)))):
            items.append({"event": event_id, "paragraph": f"p{i}", "text": texts[i]})
    rng.shuffle(items)
    for n, item in enumerate(items):
        item["id"] = f"v{n:02d}"
    write_json(SAMPLE_PATH, items)
    with open(BLANK_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "label", "text"])
        for item in items:
            w.writerow([item["id"], "", item["text"]])
    print(f"wrote {len(items)} paragraphs to {SAMPLE_PATH} and {BLANK_PATH}")
    return items


def rescore() -> dict:
    items = read_json(SAMPLE_PATH)
    texts = [i["text"] for i in items]
    runs = [[r["stance"] for r in stance_scorer.score_stances(texts)] for _ in range(2)]
    out = {"model": stance_scorer.GEMINI_MODEL, "ids": [i["id"] for i in items],
           "run1": runs[0], "run2": runs[1]}
    write_json(MODEL_PATH, out)
    print(f"scored {len(texts)} paragraphs twice -> {MODEL_PATH}")
    return out


def ranks(values: list) -> list:
    """Average ranks (1-based), ties share the mean rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(a: list, b: list):
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return None if va == 0 or vb == 0 else cov / (va * vb)


def sign(x: float) -> int:
    return 0 if abs(x) < SIGN_BAND else (1 if x > 0 else -1)


def read_labels() -> dict:
    """{id: [labels from every labels_<name>.csv]}"""
    labels = {}
    files = sorted(p for p in glob.glob(os.path.join(HERE, "labels_*.csv")) if p != BLANK_PATH)
    for path in files:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("label", "").strip():
                    labels.setdefault(row["id"], []).append(float(row["label"]))
    return labels, [os.path.basename(p) for p in files]


def report() -> dict:
    model = read_json(MODEL_PATH)
    labels, files = read_labels()
    ids = [i for i in model["ids"] if i in labels]
    if not ids:
        raise SystemExit("no hand labels yet: fill labels_<name>.csv first")
    m1 = dict(zip(model["ids"], model["run1"]))
    m2 = dict(zip(model["ids"], model["run2"]))
    human = {i: sum(labels[i]) / len(labels[i]) for i in ids}
    rho = spearman([m1[i] for i in ids], [human[i] for i in ids])
    agree = sum(sign(m1[i]) == sign(human[i]) for i in ids)
    identical = sum(m1[i] == m2[i] for i in model["ids"])
    metrics = {
        "n_labeled": len(ids), "labelers": files, "model": model["model"],
        "spearman": None if rho is None else round(rho, 3), "spearman_target": 0.6,
        "sign_agreement": round(agree / len(ids), 3), "sign_agreement_target": 0.85,
        "sign_band": SIGN_BAND,
        "rescore_identical": identical, "rescore_n": len(model["ids"]),
        "rescore_max_abs_diff": max(abs(m1[i] - m2[i]) for i in model["ids"]),
    }
    write_json(METRICS_PATH, metrics)
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Validate Tradio stance scores")
    parser.add_argument("step", choices=["sample", "rescore", "report"])
    args = parser.parse_args()
    {"sample": sample, "rescore": rescore, "report": report}[args.step]()


if __name__ == "__main__":
    main()
