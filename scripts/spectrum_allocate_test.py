#!/usr/bin/env python3
"""Terminal allocate-loop integration script (Stocklana Day 3).

Runs the human script against a live Miriam server through the Spectrum
contract, no mocks:

    1. "i just got paid 100"      -> 70/30 restatement + split chart
    2. "put 30 of this deposit into stocks" -> challenge (confirm id)
    3. "confirm <id>"             -> ALLOCATE card (or honest not-live text)
    4. --dev-sign: ephemeral keypair signs stage 1 -> receipt + chart/indexing
    5. "how am i looking"         -> one sentence + donut, or honest text

Usage:
    export MIRIAM_URL=http://localhost:8000 MIRIAM_TOKEN=<user JWT>
    export RAIL_ALLOW_DEV_SIGN=1   # server side, terminal testing only
    uv run python scripts/spectrum_allocate_test.py [--dev-sign] [--user-id ID]

Demo recordings must use a real wallet confirm, never --dev-sign.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.getenv("MIRIAM_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("MIRIAM_TOKEN", "")
OUT = os.getenv("GATEWAY_OUT_DIR", "/tmp/spectrum-gateway")


def call(body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/v1/chat/spectrum",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:500]}")
        sys.exit(2)


def show(parts: list[dict], step: str) -> dict:
    print(f"\n=== {step} ===")
    card: dict = {}
    for p in parts:
        if p["type"] == "text":
            print(f"Miriam: {p['text']}")
        elif p["type"] == "card":
            card = p
            print(
                f"CARD {p['title']} | {p['subtitle']} | {p['primary']} | "
                f"{p['amount']} from {p['source']} | flow={p.get('flow_id')}"
            )
            if p.get("image_base64"):
                os.makedirs(OUT, exist_ok=True)
                path = os.path.join(OUT, "allocate.jpg")
                open(path, "wb").write(base64.b64decode(p["image_base64"]))
                print(f"[card: allocate.jpg written to {path}]")
            if p.get("authorize_url"):
                print(f"Approve: {p['authorize_url']}")
            if p.get("confirm_id"):
                print(f"Reply: confirm {p['confirm_id']}")
        elif p["type"] == "chart":
            os.makedirs(OUT, exist_ok=True)
            path = os.path.join(OUT, p.get("filename", "portfolio.png"))
            open(path, "wb").write(base64.b64decode(p["image_base64"]))
            print(f"[chart: {p.get('filename')} written to {path}]")
    return card


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dev-sign",
        action="store_true",
        help="ephemeral keypair signs stage 1 (terminal testing only)",
    )
    ap.add_argument("--user-id", default=os.getenv("MIRIAM_USER_ID", "terminal-user"))
    args = ap.parse_args()
    if not TOKEN:
        print("MIRIAM_TOKEN is required (a user JWT for this deployment)")
        sys.exit(2)

    def turn(text: str, **extra) -> dict:
        return call(
            {
                "channel": "terminal",
                "space_id": "alloc-test",
                "user_id": args.user_id,
                "text": text,
                **extra,
            }
        )

    wallet = None
    if args.dev_sign:
        from solders.keypair import Keypair

        wallet = Keypair()
        print(f"[dev-sign] ephemeral owner {wallet.pubkey()}")

    out = turn("i just got paid 100")
    show(out["parts"], "payday")

    extra = {"wallet_address": str(wallet.pubkey())} if wallet else {}
    out = turn("put 30 of this deposit into stocks", **extra)
    show(out["parts"], "invest utterance")
    confirm_id = out.get("confirm_id", "")
    if not confirm_id:
        print("no challenge opened; stopping")
        sys.exit(1)

    out = turn(f"confirm {confirm_id}", **extra)
    card = show(out["parts"], "tap")
    if not card:
        print("(no card: backend said it plainly above; loop ends fail-closed)")
        return

    if args.dev_sign:
        from solders.transaction import VersionedTransaction

        raw = base64.b64decode(card["sign_payload"])
        tx = VersionedTransaction.from_bytes(raw)
        msg = bytes(tx.message)
        sig = wallet.sign_message(msg)
        raise SystemExit(_dev_submit(tx, sig, wallet, card, turn))
    print(
        "card ready: sign the payload in a real wallet, then submit signed_tx + flow_id"
    )

    out = turn("how am i looking")
    show(out["parts"], "portfolio")


def _dev_submit(tx, sig, wallet, card, turn) -> int:
    from solders.transaction import VersionedTransaction

    # Attach the signature to the transaction's signature slot.
    signatures = [bytes(s) for s in tx.signatures]
    # The fee payer (owner) signs slot 0 when required. Find our slot and set it.
    try:
        keys = tx.message.account_keys
        idx = list(keys).index(wallet.pubkey())
        signatures[idx] = bytes(sig)
    except (ValueError, IndexError):
        signatures[0] = bytes(sig)
    signed_tx = VersionedTransaction(tx.message, signatures)
    import base64 as b64

    out = turn(
        "", signed_tx=b64.b64encode(bytes(signed_tx)).decode(), flow_id=card["flow_id"]
    )
    show(out["parts"], "settle")
    out2 = turn("how am i looking")
    show(out2["parts"], "portfolio")
    return 0


if __name__ == "__main__":
    main()
