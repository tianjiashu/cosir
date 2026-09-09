import { describe, expect, it, vi } from "vitest";

describe("transport command identity", () => {
  it("keeps an object stable and avoids collisions after module reload", async () => {
    vi.resetModules();
    const firstConverter = await import("@/lib/assistant/converter");
    const command = {};
    const firstId = firstConverter.getOrCreateTransportCommandId(command);

    expect(firstConverter.getOrCreateTransportCommandId(command)).toBe(firstId);

    vi.resetModules();
    const secondConverter = await import("@/lib/assistant/converter");
    const secondId = secondConverter.getOrCreateTransportCommandId({});

    expect(secondId).not.toBe(firstId);
    expect(secondId).toMatch(/^transport-[0-9a-f-]{36}$/);
  });
});
