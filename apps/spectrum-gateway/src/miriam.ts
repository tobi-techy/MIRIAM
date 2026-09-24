/**
 * Miriam client: POSTs gateway turns to the Python brain and returns parts.
 *
 * Contract: POST {MIRIAM_URL}/api/v1/chat/spectrum
 *   {channel, space_id, user_id, text, wallet_address?, confirm_id?, yes?,
 *    signed_tx?, flow_id?}
 * -> {parts: [{type: text|card|chart, ...}]}
 *
 * Auth is the user's Rail JWT (MIRIAM_TOKEN). The gateway never invents card
 * copy: it only renders the parts Miriam returns.
 */

export type Part =
  | { type: "text"; text: string }
  | {
      type: "card";
      kind: string;
      title: string;
      subtitle: string;
      primary: string;
      amount: string;
      source: string;
      cta: string;
      flow_id: string;
      confirm_id: string;
      authorize_url?: string;
      expires_in_sec: number;
      image_base64?: string;
    }
  | {
      type: "chart";
      spec: { kind: string; title: string; slices: unknown[] };
      filename: string;
      mime: string;
      image_base64: string;
    };

export interface MiriamRequest {
  channel: "imessage" | "whatsapp" | "terminal";
  space_id: string;
  user_id: string;
  sender_id?: string;
  text: string;
  wallet_address?: string;
  confirm_id?: string;
  yes?: boolean;
  signed_tx?: string;
  flow_id?: string;
}

const base = (process.env.MIRIAM_URL ?? "http://localhost:8000").replace(/\/$/, "");
const token = process.env.MIRIAM_TOKEN ?? "";

export async function callMiriam(req: MiriamRequest): Promise<Part[]> {
  const res = await fetch(`${base}/api/v1/chat/spectrum`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`miriam ${res.status}: ${body.slice(0, 300)}`);
  }
  const data = (await res.json()) as { parts: Part[] };
  if (!Array.isArray(data.parts)) throw new Error("miriam returned no parts");
  return data.parts;
}
