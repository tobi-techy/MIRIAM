/**
 * Initiate a cloud iMessage DM (shared-pool lines can't receive first —
 * the line must message the user before they can reply in-thread).
 *
 * Usage: bun src/initiate.ts +2349164904178 "Miriam here — ..."
 */
import { Spectrum, text } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

const to = process.argv[2];
const body = process.argv.slice(3).join(" ") || "Miriam here — reply to test the line.";
if (!to) {
  console.error("usage: bun src/initiate.ts <phone-or-email> [message]");
  process.exit(1);
}

const spec = await Spectrum({
  projectId: process.env.SPECTRUM_PROJECT_ID!,
  projectSecret: process.env.SPECTRUM_PROJECT_SECRET!,
  providers: [imessage.config()],
});

const im = imessage(spec);
const user = await im.user(to);
const dm = await im.space.create(user);
await dm.send(text(body));
console.log(`[initiate] sent to ${to}`);
await spec.stop();
