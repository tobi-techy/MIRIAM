import { describe, expect, test } from "bun:test";
import { miriamChannel, miriamSpace, shouldHandle } from "./guard";

const BOOT = 1_000_000;

describe("inbound guardrails", () => {
  test("own echoes and pre-boot replays never reach Miriam", () => {
    expect(
      shouldHandle(
        { platform: "terminal", direction: "outbound", sender: "me", timestampMs: BOOT + 1 },
        BOOT,
        [],
      ),
    ).toBe("own-echo");
    expect(
      shouldHandle(
        { platform: "local_imessage", direction: "inbound", sender: "+1555", timestampMs: BOOT - 1 },
        BOOT,
        ["+1555"],
      ),
    ).toBe("pre-boot");
  });

  test("local imessage requires the allowlist; terminal does not", () => {
    expect(
      shouldHandle(
        { platform: "local_imessage", direction: "inbound", sender: "+1999", timestampMs: BOOT + 1 },
        BOOT,
        ["+1555"],
      ),
    ).toBe("not-allowlisted");
    expect(
      shouldHandle(
        { platform: "local_imessage", direction: "inbound", sender: " +1555 ", timestampMs: BOOT + 1 },
        BOOT,
        ["+1555"],
      ),
    ).toBe("ok");
    expect(
      shouldHandle(
        { platform: "terminal", direction: "inbound", sender: "me", timestampMs: BOOT + 1 },
        BOOT,
        [],
      ),
    ).toBe("ok");
  });

  test("channel and space mapping", () => {
    expect(miriamChannel("local_imessage", "terminal")).toBe("imessage");
    expect(miriamChannel("terminal", "terminal")).toBe("terminal");
    expect(miriamSpace("local_imessage", "abc")).toBe("local_imessage:abc");
  });
});
