# Solana receipts

`tradio/receipts.py` writes Tradio's results onto Solana **devnet**, which is free and
uses no real money. The page then shows a "◎ Verified on Solana" badge linking to the
explorer.

```
python solana_proof.py --address             # wallet (solana_keypair.json, gitignored)
# fund it once at https://faucet.solana.com (pick Devnet)
python -m tradio.receipts data               # hash 400+ result files, anchor the root hash
python -m tradio.receipts forecast <event>   # anchor the learner's prediction (do it BEFORE T0 on a live event)
python -m tradio.receipts verify             # rehash locally + read the memos back on-chain
```

If any number in any cached file changes, the root hash changes and `verify` names the
file that changed. For past events, a receipt only proves **when we published**. It
doesn't prove foresight. A forecast anchored before a live speech's T0 does prove the
prediction came first, and `verify` says which case each receipt is.
