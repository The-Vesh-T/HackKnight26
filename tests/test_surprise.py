"""Tests for role 3's tradio/surprise.py and tradio/validation/validate.py (no Gemini).

    python -m unittest tests.test_surprise
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tradio import surprise  # noqa: E402
from tradio.validation import validate  # noqa: E402

BASE = {"n": 100, "mean": 0.2, "stdev": 0.3, "built_by": "test"}
POLICY = ("I would support lowering the policy rate at our next meeting if inflation "
          "pressures remain contained, because the labor market has softened.")
CHATTER = ("It is a pleasure to be here in Prague today and to see so many friends from "
           "the central banking community gathered for this conference.")


def fake_scores(stances):
    """score_stances stand-in: returns the stances it is told to, in order."""
    it = iter(stances)
    return lambda texts: [{"stance": next(it), "summary": "s"} for _ in texts]


class TestRelevance(unittest.TestCase):
    def test_policy_paragraph_is_relevant(self):
        self.assertTrue(surprise.is_relevant(POLICY))

    def test_short_or_offtopic_is_not(self):
        self.assertFalse(surprise.is_relevant("Lower the policy rate."))
        self.assertFalse(surprise.is_relevant(CHATTER))

    def test_sentences_drop_fragments(self):
        text = "Thank you. " + POLICY + " Inflation is still above our goal of two percent."
        out = surprise.sentences(text)
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].startswith("I would support"))

    def test_sentences_split_after_footnote_marker(self):
        text = ("I believe we should reduce our policy rate at the next meeting.1 I used to "
                "tell my colleagues that presentations are not murder mysteries at all.")
        out = surprise.sentences(text)
        self.assertEqual(out[0], "I believe we should reduce our policy rate at the next meeting.")
        self.assertTrue(out[1].startswith("I used to"))


class TestBuildParagraphs(unittest.TestCase):
    def scores(self):
        return {"model": "m", "baseline": BASE, "top_sentences": [
                    {"text": "a mild sentence", "stance": 0.0},
                    {"text": "the dovish sentence", "stance": -0.6}],
                "paragraphs": [
                    {"id": "p0", "text": CHATTER, "relevant": False, "stance": None, "summary": None},
                    {"id": "p1", "text": POLICY, "relevant": True, "stance": -0.5, "summary": "s"},
                    {"id": "p2", "text": POLICY, "relevant": True, "stance": 0.4, "summary": "s"}]}

    def test_z_and_top(self):
        out = surprise.build_paragraphs(self.scores())
        p = {x["id"]: x for x in out["paragraphs"]}
        self.assertIsNone(p["p0"]["z"])
        self.assertAlmostEqual(p["p1"]["z"], (-0.5 - 0.2) / 0.3, places=3)
        self.assertEqual(out["top_paragraph"], "p1")
        self.assertTrue(p["p1"]["top"])
        self.assertFalse(p["p2"]["top"])

    def test_key_sentence_follows_top_direction(self):
        out = surprise.build_paragraphs(self.scores())
        top = next(x for x in out["paragraphs"] if x["top"])
        self.assertEqual(top["key_sentence"], "the dovish sentence")
        self.assertTrue(all(x["key_sentence"] is None for x in out["paragraphs"] if not x["top"]))

    def test_no_relevant_paragraphs(self):
        s = self.scores()
        s["paragraphs"] = s["paragraphs"][:1]
        out = surprise.build_paragraphs(s)
        self.assertIsNone(out["top_paragraph"])

    def test_contract_fields(self):
        out = surprise.build_paragraphs(self.scores())
        for p in out["paragraphs"]:
            for key in ("id", "text", "stance", "z", "relevant", "key_sentence"):
                self.assertIn(key, p)


class TestScoreEvent(unittest.TestCase):
    def test_scores_only_relevant_then_top_sentences(self):
        speech = {"segments": [{"text": CHATTER}, {"text": POLICY}, {"text": POLICY + " " + POLICY}]}
        calls = []

        def fake(texts):
            calls.append(list(texts))
            return [{"stance": -0.4 - 0.1 * i, "summary": "s"} for i in range(len(texts))]

        with mock.patch.object(surprise.stance_scorer, "load_store", return_value={"bowman": BASE}), \
             mock.patch.object(surprise.build_baselines, "ensure_downloaded", return_value=speech), \
             mock.patch.object(surprise.stance_scorer, "score_stances", side_effect=fake):
            out = surprise.score_event("e", {"speaker": "bowman", "speech_id": "x"})
        self.assertEqual(calls[0], [POLICY, POLICY + " " + POLICY])
        self.assertFalse(out["paragraphs"][0]["relevant"])
        self.assertEqual(len(out["top_sentences"]), 2)   # top is p2, split into its two sentences
        self.assertEqual(out["baseline"]["mean"], 0.2)


class TestWriteTimeline(unittest.TestCase):
    def test_updates_speech_and_keeps_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, web = os.path.join(tmp, "data"), os.path.join(tmp, "web")
            os.makedirs(os.path.join(data, "e"))
            path = os.path.join(data, "e", "timeline.json")
            with open(path, "w") as f:
                json.dump({"event_id": "e", "speech": {"source_url": "u", "paragraphs": []},
                           "quotes": [1]}, f)
            with mock.patch.object(surprise, "DATA_DIR", data), \
                 mock.patch.object(surprise, "WEB_DATA_DIR", web):
                surprise.write_timeline("e", {"paragraphs": [{"id": "p0"}], "top_paragraph": "p0"})
            with open(path) as f:
                t = json.load(f)
            self.assertEqual(t["speech"]["source_url"], "u")
            self.assertEqual(t["speech"]["top_paragraph"], "p0")
            self.assertEqual(t["quotes"], [1])


class TestValidationMetrics(unittest.TestCase):
    def test_spearman(self):
        self.assertAlmostEqual(validate.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(validate.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(validate.spearman([1, 1, 1], [1, 2, 3]))

    def test_ranks_ties(self):
        self.assertEqual(validate.ranks([0.5, 0.1, 0.5]), [2.5, 1.0, 2.5])

    def test_sign_band(self):
        self.assertEqual(validate.sign(0.1), 0)
        self.assertEqual(validate.sign(-0.3), -1)
        self.assertEqual(validate.sign(0.3), 1)

    def test_report_reads_label_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = os.path.join(tmp, "model_scores.json")
            with open(model, "w") as f:
                json.dump({"model": "m", "ids": ["v00", "v01", "v02"],
                           "run1": [-0.5, 0.0, 0.6], "run2": [-0.5, 0.1, 0.6]}, f)
            for name, labels in (("a", [-0.6, 0.1, 0.5]), ("b", [-0.4, -0.1, 0.7])):
                with open(os.path.join(tmp, f"labels_{name}.csv"), "w") as f:
                    f.write("id,label,text\n" + "".join(
                        f"v0{i},{x},t\n" for i, x in enumerate(labels)))
            with open(os.path.join(tmp, "labels_blank.csv"), "w") as f:
                f.write("id,label,text\nv00,,t\n")
            with mock.patch.multiple(validate, HERE=tmp, MODEL_PATH=model,
                                     BLANK_PATH=os.path.join(tmp, "labels_blank.csv"),
                                     METRICS_PATH=os.path.join(tmp, "metrics.json")):
                m = validate.report()
        self.assertEqual(m["n_labeled"], 3)
        self.assertEqual(m["labelers"], ["labels_a.csv", "labels_b.csv"])
        self.assertAlmostEqual(m["spearman"], 1.0)
        self.assertEqual(m["sign_agreement"], 1.0)
        self.assertEqual(m["rescore_identical"], 2)


if __name__ == "__main__":
    unittest.main()
