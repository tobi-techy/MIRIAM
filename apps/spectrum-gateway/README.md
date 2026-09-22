# Spectrum gateway

One agent loop, many providers. Terminal today, iMessage next.

## Run (terminal)

```bash
cd apps/spectrum-gateway
cp .env.example .env   # set MIRIAM_TOKEN (a user JWT)
bun install
bun start
```

Type the allocate script:

```
i just got paid 100
put 30 of this deposit into stocks
confirm <code>
how am i looking
```

Card and chart images land in `$GATEWAY_OUT_DIR` (`/tmp/spectrum-gateway`).

## Dev signing (terminal testing only)

```bash
RAIL_ALLOW_DEV_SIGN=1  # server side
bun src/index.ts --dev-sign
```

Generates an ephemeral Solana keypair, sends its address with invest
utterances, signs the stage-1 transaction locally (real ed25519), and
submits. Demo recordings must use a real wallet confirm, never this flag.

## iMessage — local line (Day 4, no Photon project needed)

The gateway reads your Mac's Messages database and replies in-thread:

```bash
# 1. Grant Full Disk Access to your terminal:
#    System Settings → Privacy & Security → Full Disk Access → add Terminal
#    (bun inherits it from the terminal that launches it)
# 2. On first send, approve the Automation prompt (Terminal → Messages).
cp .env.example .env   # set MIRIAM_TOKEN, IMESSAGE_ALLOW_FROM=<your number>
GATEWAY_IMESSAGE_LOCAL=1 IMESSAGE_ALLOW_FROM=+15551234567 bun start
```

Text the Mac's number `put 30 of this deposit into stocks` from the
allowlisted line and the allocate card + chart arrive as iMessage
attachments. Anyone not on `IMESSAGE_ALLOW_FROM` is ignored, own echoes and
pre-boot replays never reach Miriam, and the local line maps to channel
`imessage` server-side.

Verified: send leg works (signed-in iMessage service resolves via
AppleScript); Buffer attachments are temp-filed and delivered; guardrails
unit-tested (`bun test`). The read leg is gated on Full Disk Access —
without it the gateway exits with the exact grant instructions.

## iMessage — cloud line (needs Photon project)

Set `SPECTRUM_PROJECT_ID` / `SPECTRUM_PROJECT_SECRET` from
https://app.photon.codes and restart: the iMessage provider joins the same
loop. Layer A cards (JPEG + optional authorize URL) work on every platform.
Layer B (live in-thread Face ID via `customizedMiniApp` + a Messages
extension) is out of scope this sprint.
