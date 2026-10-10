"""
tradio/receipts.py
==================
Solana receipts for Tradio: proof that the numbers on screen were fixed at a known
time and haven't been edited since. Uses solana_proof.py (Memo program, DEVNET by
default: free, no real money).

    python -m tradio.receipts data              # hash every cached result file, anchor the root
    python -m tradio.receipts forecast <event>  # learner's P(react) for a live event, anchored before T0
    python -m tradio.receipts verify            # recompute local hashes, read the memos back on-chain
    python -m tradio.receipts verify --offline  # local hashes only

Two kinds of receipt:
  * data       SHA-256 of every timeline.json / analysis.json / meta.json / model.json /
               validation score file, combined into one root hash. The memo is
               `tradio|data|<root>|<n files>`. Anyone can rehash the repo and check the root
               against the memo and its block time. If a single number in any file changes,
               the root changes.
  * forecast   the learner's per-contract P(react) for an event, hashed and anchored. Do
               this BEFORE T0 on a live event, and the block time proves the prediction
               came first. For past events it only proves when we published, not foresight;
               verify says which.

Written to tradio/data/receipts/ (committed): data.json, forecast_<event>.json. The wallet
is solana_keypair.json (gitignored), created on first run; `python solana_proof.py --airdrop`
funds it with free devnet SOL.
"""

import argparse
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tradio.common import DATA_DIR, WEB_DATA_DIR, parse_ts, read_json, write_json  # noqa: E402

RECEIPTS_DIR = os.path.join(DATA_DIR, "receipts")
DATA_PATTERNS = ("*/timeline.json", "*/analysis.json", "history/*/analysis.json",
                 "history/*/meta.json", "model.json", "playbook.json")
EXTRA_PATTERNS = (os.path.join(ROOT, "tradio", "validation", "scores", "*.json"),
                  os.path.join(ROOT, "tradio", "validation", "model_scores.json"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def data_files() -> list:
    paths = set()
    for pattern in DATA_PATTERNS:
        paths.update(glob.glob(os.path.join(DATA_DIR, pattern)))
    for pattern in EXTRA_PATTERNS:
        paths.update(glob.glob(pattern))
    return sorted(os.path.relpath(p, ROOT).replace(os.sep, "/") for p in paths)


def manifest() -> dict:
    """{relative path: sha256} plus a root = sha256 of the sorted "path  hash" lines."""
    files = {rel: sha256_file(os.path.join(ROOT, rel)) for rel in data_files()}
    lines = "".join(f"{rel}  {digest}\n" for rel, digest in sorted(files.items()))
    return {"root": hashlib.sha256(lines.encode("utf-8")).hexdigest(), "n_files": len(files),
            "files": files}


def _anchor(memo: str) -> dict:
    import solana_proof
    sig = solana_proof.send_memo(memo)
    return {"cluster": solana_proof.SOLANA_CLUSTER, "signature": sig,
            "explorer": solana_proof.explorer_url(sig), "memo": memo}


def anchor_data(send=True) -> dict:
    m = manifest()
    memo = f"tradio|data|{m['root']}|{m['n_files']}"
    receipt = {"kind": "data", "created_at": _now(), **m,
               "chain": _anchor(memo) if send else {"memo": memo, "signature": None}}
    write_json(os.path.join(RECEIPTS_DIR, "data.json"), receipt)
    # small copy for the page's "Verified on Solana" badge (no file list)
    write_json(os.path.join(WEB_DATA_DIR, "receipts.json"),
               {k: receipt[k] for k in ("root", "n_files", "created_at", "chain")})
    return receipt


def anchor_forecast(event_id: str, send=True) -> dict:
    from tradio import learner
    for base in (os.path.join(DATA_DIR, event_id), os.path.join(DATA_DIR, "history", event_id)):
        if os.path.isdir(base):
            break
    else:
        raise SystemExit(f"no event folder for {event_id}")
    pred = learner.predict(base)
    if pred is None:
        raise SystemExit(f"learner has no prediction for {event_id}")
    digest = sha256_json(pred)
    memo = f"tradio|forecast|{event_id}|{pred['model']}|{digest}"
    receipt = {"kind": "forecast", "event_id": event_id, "created_at": _now(), "sha256": digest,
               "prediction": pred, "chain": _anchor(memo) if send else {"memo": memo, "signature": None}}
    write_json(os.path.join(RECEIPTS_DIR, f"forecast_{event_id}.json"), receipt)
    return receipt


def _t0(event_id: str):
    for path in (os.path.join(DATA_DIR, event_id, "timeline.json"),
                 os.path.join(DATA_DIR, "history", event_id, "meta.json")):
        if os.path.isfile(path):
            return read_json(path).get("t0")
    return None


def verify(offline=False) -> list:
    """[(receipt file, ok, message)]: local hashes match the receipt, and the memo is on-chain."""
    out = []
    for path in sorted(glob.glob(os.path.join(RECEIPTS_DIR, "*.json"))):
        r = read_json(path)
        name = os.path.basename(path)
        if r["kind"] == "data":
            now = manifest()
            changed = sorted(k for k in set(now["files"]) | set(r["files"])
                             if now["files"].get(k) != r["files"].get(k))
            ok = now["root"] == r["root"]
            msg = "root matches" if ok else f"{len(changed)} file(s) changed since: {', '.join(changed[:5])}"
        else:
            ok = sha256_json(r["prediction"]) == r["sha256"]
            msg = "prediction hash matches" if ok else "prediction was edited"
        if not offline and r["chain"].get("signature"):
            import solana_proof
            chain = solana_proof.verify(r["chain"]["signature"])
            on_chain = chain.get("memo") == r["chain"]["memo"]
            ok = ok and on_chain
            msg += f"; memo {'found' if on_chain else 'NOT found'} on {r['chain']['cluster']}"
            block_time = chain.get("block_time")
            if r["kind"] == "forecast" and block_time and _t0(r["event_id"]):
                t0 = parse_ts(_t0(r["event_id"])).timestamp()
                msg += "; anchored BEFORE T0" if block_time < t0 else "; anchored after T0 (not a forecast)"
        elif not r["chain"].get("signature"):
            msg += "; not anchored on-chain"
        out.append((name, ok, msg))
    return out


def main():
    ap = argparse.ArgumentParser(description="Solana receipts for Tradio's results")
    ap.add_argument("step", choices=["data", "forecast", "verify"])
    ap.add_argument("event", nargs="?")
    ap.add_argument("--offline", action="store_true", help="verify: skip the on-chain check")
    ap.add_argument("--dry-run", action="store_true", help="write the receipt without sending")
    a = ap.parse_args()
    if a.step == "data":
        r = anchor_data(send=not a.dry_run)
        print(f"root {r['root']} over {r['n_files']} files")
        print(r["chain"].get("explorer") or f"memo (not sent): {r['chain']['memo']}")
    elif a.step == "forecast":
        if not a.event:
            raise SystemExit("usage: python -m tradio.receipts forecast <event_id>")
        r = anchor_forecast(a.event, send=not a.dry_run)
        print(f"{a.event}: {r['prediction']['model']} sha256 {r['sha256']}")
        print(r["chain"].get("explorer") or f"memo (not sent): {r['chain']['memo']}")
    else:
        results = verify(offline=a.offline)
        for name, ok, msg in results:
            print(f"  {'OK  ' if ok else 'FAIL'} {name}: {msg}")
        if not results:
            print("no receipts yet: python -m tradio.receipts data")
        sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == "__main__":
    main()
