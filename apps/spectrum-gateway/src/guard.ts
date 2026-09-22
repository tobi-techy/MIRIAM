/** Inbound guardrails: what may reach Miriam. Pure, unit-tested. */

export interface Inbound {
  platform: string;
  direction: string;
  sender: string;
  timestampMs: number;
}

export function shouldHandle(
  msg: Inbound,
  bootTimeMs: number,
  allowFrom: string[],
): "ok" | "own-echo" | "pre-boot" | "not-allowlisted" {
  if (msg.direction !== "inbound") return "own-echo";
  if (msg.timestampMs && msg.timestampMs < bootTimeMs) return "pre-boot";
  if (
    msg.platform === "local_imessage" &&
    !allowFrom.includes(msg.sender.trim().toLowerCase())
  ) {
    return "not-allowlisted";
  }
  return "ok";
}

export function miriamChannel(platform: string, fallback: string): string {
  return platform === "local_imessage" ? "imessage" : fallback;
}

export function miriamSpace(platform: string, spaceId: string): string {
  return `${platform}:${spaceId}`;
}
