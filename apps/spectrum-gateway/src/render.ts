/**
 * Part renderer: Miriam parts -> Spectrum content.
 *
 * Layer A (this sprint, all platforms): restatement text, a server-rendered
 * card JPEG attachment, a tappable authorize URL when the deployment has one,
 * and chart PNG attachments. The user replies `confirm <code>` or signs.
 *
 * Layer B (after the hackathon): an Apple Messages extension plus
 * customizedMiniApp({live: true, ...}) with edit() flipping Approve->Approved
 * for in-thread Face ID. Requires an Apple Developer team, an extension
 * bundle, and a Photon cloud iMessage line. Out of scope here.
 */
import { app, attachment, text, type Space } from "spectrum-ts";
import { writeFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { Part } from "./miriam.js";

const outDir = process.env.GATEWAY_OUT_DIR ?? join(tmpdir(), "spectrum-gateway");

async function saveImage(name: string, b64: string): Promise<string> {
  await mkdir(outDir, { recursive: true });
  const path = join(outDir, `${Date.now()}-${name}`);
  await writeFile(path, Buffer.from(b64, "base64"));
  return path;
}

export function inboundText(message: unknown): string {
  const m = message as { content?: { type?: string; text?: string } };
  if (m?.content?.type === "text") return m.content.text ?? "";
  return "";
}

/** Render Miriam parts into a space. Returns card/chart metadata for signing. */
export async function renderMiriam(
  space: Space,
  parts: Part[],
): Promise<{ flow_id?: string; sign_payload?: string; confirm_id?: string }> {
  const meta: { flow_id?: string; sign_payload?: string; confirm_id?: string } = {};
  for (const part of parts) {
    if (part.type === "text") {
      await space.send(text(part.text));
    } else if (part.type === "card") {
      const restate =
        `${part.title} — ${part.subtitle}\n${part.primary}\n${part.amount} from ${part.source}.`;
      await space.send(text(restate));
      if (part.image_base64) {
        const path = await saveImage("allocate.jpg", part.image_base64);
        try {
          await space.send(
            attachment(Buffer.from(part.image_base64, "base64"), {
              name: "allocate.jpg",
              mimeType: "image/jpeg",
            }),
          );
        } catch {
          await space.send(text(`[card image: ${path}]`));
        }
      }
      if (part.authorize_url) {
        await space.send(app(part.authorize_url));
      }
      if (part.confirm_id) {
        meta.confirm_id = part.confirm_id;
        await space.send(text(`Reply: confirm ${part.confirm_id}`));
      }
      if (part.flow_id) meta.flow_id = part.flow_id;
      const signPayload = (part as { sign_payload?: string }).sign_payload;
      if (signPayload) meta.sign_payload = signPayload;
    } else if (part.type === "chart") {
      const path = await saveImage(part.filename || "portfolio.png", part.image_base64);
      try {
        await space.send(
          attachment(Buffer.from(part.image_base64, "base64"), {
            name: part.filename || "portfolio.png",
            mimeType: part.mime || "image/png",
          }),
        );
      } catch {
        await space.send(text(`[chart: ${part.filename} written to ${path}]`));
      }
      // Terminal provider prints attachments; belt and suspenders for logs:
      console.log(`[chart: ${part.filename} written to ${path}]`);
    }
  }
  return meta;
}
