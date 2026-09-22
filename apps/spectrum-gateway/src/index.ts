/**
 * Spectrum gateway: one agent loop, many providers.
 *
 * Terminal is the day-one test harness (projectless, no credentials).
 * iMessage joins the same loop as soon as SPECTRUM_PROJECT_ID /
 * SPECTRUM_PROJECT_SECRET (https://app.photon.codes) are set; WhatsApp later
 * via whatsappBusiness.config({...}) in the same providers array.
 *
 * Flow per message: inbound text -> POST Miriam /api/v1/chat/spectrum ->
 * render parts (text, allocate card + JPEG, chart PNG, authorize URL).
 *
 * Dev signing (terminal only): --dev-sign generates an ephemeral Solana
 * keypair, sends its address with invest utterances, signs the stage-1
 * transaction locally with real ed25519, and submits the signature. The
 * server only accepts text-carried signatures when RAIL_ALLOW_DEV_SIGN=1,
 * and demo recordings must use a real wallet instead.
 */
import { Spectrum } from "spectrum-ts";
import { terminal } from "spectrum-ts/providers/terminal";
import { imessage } from "spectrum-ts/providers/imessage";
import { localIMessage } from "@spectrum-ts/imessage-local";
import { Keypair, VersionedTransaction } from "@solana/web3.js";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { readFile, rm, writeFile } from "node:fs/promises";
import { callMiriam, type MiriamRequest } from "./miriam.js";
import { inboundText, renderMiriam } from "./render.js";
import { miriamChannel, miriamSpace, shouldHandle } from "./guard.js";

const MIRIAM_USER = process.env.MIRIAM_USER_ID ?? "terminal-user";
const CHANNEL = (process.env.GATEWAY_CHANNEL ?? "terminal") as
  | "imessage"
  | "whatsapp"
  | "terminal";
const DEV_SIGN = process.argv.includes("--dev-sign");

const devKeypair = DEV_SIGN ? Keypair.generate() : null;
if (devKeypair) {
  console.log(`[dev-sign] ephemeral owner ${devKeypair.publicKey.toBase58()}`);
  console.log("[dev-sign] server must run with RAIL_ALLOW_DEV_SIGN=1");
}

// Singleton: two gateways watching one inbox would double-answer every
// message (and double-tap challenges). A money bot must never run twinned.
const lockFile = join(tmpdir(), "spectrum-gateway.lock");
try {
  const stale = await readFile(lockFile, "utf8").catch(() => "");
  if (stale.trim()) {
    const [pid] = stale.trim().split(":");
    try {
      process.kill(Number(pid), 0);
      console.error(`[gateway] another instance is running (pid ${pid}); refusing to start`);
      process.exit(1);
    } catch {
      // stale lock, take over
    }
  }
} catch {
  // no lock, take over
}
await writeFile(lockFile, `${process.pid}:${Date.now()}`);
const releaseLock = () => rm(lockFile, { force: true }).catch(() => {});
process.on("SIGINT", async () => {
  await releaseLock();
  process.exit(0);
});
process.on("SIGTERM", async () => {
  await releaseLock();
  process.exit(0);
});

const imessageLive = Boolean(
  process.env.SPECTRUM_PROJECT_ID && process.env.SPECTRUM_PROJECT_SECRET,
);
// Local macOS iMessage (reads ~/Library/Messages/chat.db, sends via
// Messages.app). Needs Full Disk Access for this process (probe below) and,
// as a safety rail, an explicit sender allowlist: a money bot must never
// answer every conversation on the account.
const imessageLocal = process.env.GATEWAY_IMESSAGE_LOCAL === "1";
const allowFrom = (process.env.IMESSAGE_ALLOW_FROM ?? "")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);

if (imessageLocal && allowFrom.length === 0) {
  console.error(
    "[gateway] refusing local iMessage without IMESSAGE_ALLOW_FROM "
    + "(comma-separated phones/emails of the one test line)",
  );
  process.exit(1);
}

if (imessageLocal) {
  const db = join(homedir(), "Library", "Messages", "chat.db");
  try {
    const { Database } = await import("bun:sqlite");
    const probe = new Database(db, { readonly: true });
    probe.query("select count(*) as n from message").get();
    probe.close();
    console.log("[gateway] chat.db readable (Full Disk Access ok)");
  } catch (err) {
    console.error(
      `[gateway] cannot read ${db}: ${(err as Error).message}\n`
      + "Grant Full Disk Access: System Settings → Privacy & Security → "
      + "Full Disk Access → add your terminal app, then restart the gateway.",
    );
    process.exit(1);
  }
}

const providers = [
  terminal.config(),
  ...(imessageLive ? [imessage.config()] : []),
  ...(imessageLocal ? [localIMessage.config()] : []),
];
if (imessageLive) console.log("[gateway] cloud imessage provider live");
if (imessageLocal) {
  console.log(`[gateway] local imessage live (allow-from: ${allowFrom.join(", ")})`);
}
if (!imessageLive && !imessageLocal) {
  console.log("[gateway] terminal only (no iMessage configured)");
}

// Projectless when only the terminal provider is configured (no credentials
// sent); Photon project auth only when iMessage is live.
const spec = imessageLive
  ? await Spectrum({
      projectId: process.env.SPECTRUM_PROJECT_ID ?? "",
      projectSecret: process.env.SPECTRUM_PROJECT_SECRET ?? "",
      providers,
    })
  : await Spectrum({ providers });

console.log("[gateway] ready. Type the allocate script, e.g. 'i just got paid 100'.");

const bootTime = Date.now();
const norm = (s: string) => s.trim().toLowerCase();

for await (const [space, message] of spec.messages) {
  const platform = (message as { platform?: string }).platform ?? "";
  const direction = (message as { direction?: string }).direction ?? "inbound";
  const sender = (
    (message as { sender?: { id?: string } }).sender?.id ?? ""
  ).toLowerCase();
  const ts = (message as { timestamp?: Date }).timestamp?.getTime?.() ?? 0;

  // Guardrails: our own echoes, pre-boot replays, and (for local iMessage)
  // anyone not on the allowlist never reach Miriam.
  const verdict = shouldHandle(
    {
      platform,
      direction,
      sender,
      timestampMs: ts,
    },
    bootTime,
    allowFrom,
  );
  if (verdict === "not-allowlisted") {
    console.log(`[gateway] ignoring local imessage from non-allowlisted ${sender}`);
  }
  if (verdict !== "ok") continue;

  if (process.env.DEBUG_INBOUND === "1") {
    try {
      console.log("[inbound]", JSON.stringify(message));
    } catch {
      console.log("[inbound] (unserializable)", platform, direction);
    }
  }

  await space.responding(async () => {
    const req: MiriamRequest = {
      channel: miriamChannel(platform, CHANNEL) as MiriamRequest["channel"],
      space_id: miriamSpace(platform, space.id),
      user_id: MIRIAM_USER,
      text: inboundText(message),
      ...(devKeypair ? { wallet_address: devKeypair.publicKey.toBase58() } : {}),
    };
    try {
      const parts = await callMiriam(req);
      const meta = await renderMiriam(space, parts);
      // Dev-sign: sign the stage-1 transaction and submit the signature.
      if (DEV_SIGN && devKeypair && meta.sign_payload && meta.flow_id) {
        const tx = VersionedTransaction.deserialize(
          Buffer.from(meta.sign_payload, "base64"),
        );
        tx.sign([devKeypair]);
        const signed = Buffer.from(tx.serialize()).toString("base64");
        console.log(`[dev-sign] submitting signature for flow ${meta.flow_id}`);
        const follow = await callMiriam({
          channel: miriamChannel(platform, CHANNEL) as MiriamRequest["channel"],
          space_id: miriamSpace(platform, space.id),
          user_id: MIRIAM_USER,
          text: "",
          signed_tx: signed,
          flow_id: meta.flow_id,
        });
        await renderMiriam(space, follow);
      } else if (meta.confirm_id && !meta.sign_payload) {
        console.log(`[gateway] open challenge: confirm ${meta.confirm_id}`);
      }
    } catch (err) {
      console.error("[gateway] turn failed:", (err as Error).message);
    }
  });
}
