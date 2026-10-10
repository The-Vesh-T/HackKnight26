"""Tests for tradio/receipts.py (no Solana network).

    python -m unittest tests.test_receipts
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tradio import receipts  # noqa: E402


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        data = os.path.join(root, "tradio", "data")
        os.makedirs(os.path.join(data, "e1"))
        for name, obj in (("e1/timeline.json", {"t0": "2025-06-23T14:00:00Z"}),
                          ("e1/analysis.json", {"markets": {}}), ("model.json", {"champion": "x"})):
            with open(os.path.join(data, name), "w") as f:
                json.dump(obj, f)
        self.data = data
        self.patch = mock.patch.multiple(
            receipts, ROOT=root, DATA_DIR=data, RECEIPTS_DIR=os.path.join(data, "receipts"),
            WEB_DATA_DIR=os.path.join(root, "web", "data", "tradio"), EXTRA_PATTERNS=())
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_manifest_is_deterministic_and_covers_files(self):
        a, b = receipts.manifest(), receipts.manifest()
        self.assertEqual(a["root"], b["root"])
        self.assertEqual(sorted(a["files"]), ["tradio/data/e1/analysis.json",
                                              "tradio/data/e1/timeline.json", "tradio/data/model.json"])

    def test_verify_catches_an_edit(self):
        r = receipts.anchor_data(send=False)
        self.assertTrue(r["chain"]["memo"].startswith("tradio|data|" + r["root"]))
        self.assertTrue(receipts.verify(offline=True)[0][1])
        with open(os.path.join(self.data, "e1", "analysis.json"), "w") as f:
            json.dump({"markets": {"m": 1}}, f)
        name, ok, msg = receipts.verify(offline=True)[0]
        self.assertFalse(ok)
        self.assertIn("tradio/data/e1/analysis.json", msg)

    def test_web_copy_has_no_file_list(self):
        receipts.anchor_data(send=False)
        with open(os.path.join(self.tmp.name, "web", "data", "tradio", "receipts.json")) as f:
            web = json.load(f)
        self.assertNotIn("files", web)
        self.assertIn("root", web)

    def test_forecast_hash_and_before_t0(self):
        pred = {"event": "e1", "model": "ensemble", "markets": {"m": {"p_react": 0.4}}}
        fake_chain = {"memo": None, "block_time": 0}
        with mock.patch("tradio.learner.predict", return_value=pred), \
             mock.patch.object(receipts, "_anchor", lambda memo: {
                 "cluster": "devnet", "signature": "sig", "explorer": "x", "memo": memo}):
            r = receipts.anchor_forecast("e1")
        self.assertEqual(r["sha256"], receipts.sha256_json(pred))
        fake_chain["memo"] = r["chain"]["memo"]
        fake_chain["block_time"] = 1750687200 - 60      # one minute before T0 (14:00Z)
        solana = mock.MagicMock(verify=lambda sig: fake_chain)
        with mock.patch.dict(sys.modules, {"solana_proof": solana}):
            name, ok, msg = receipts.verify()[0]
        self.assertTrue(ok)
        self.assertIn("anchored BEFORE T0", msg)


if __name__ == "__main__":
    unittest.main()
