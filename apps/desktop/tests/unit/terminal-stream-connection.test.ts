import { describe, expect, it } from "vitest";

import {
  acceptsTerminalGeneration,
  shouldResyncForSequence,
} from "@/components/terminal/terminal-stream-connection";

describe("terminal preview sequence fencing", () => {
  it("accepts the first frame from a fresh ring-buffer attach", () => {
    expect(shouldResyncForSequence(0, 42, false)).toBe(false);
  });

  it("requires contiguous frames after a cursor has been established", () => {
    expect(shouldResyncForSequence(7, 8, true)).toBe(false);
    expect(shouldResyncForSequence(7, 9, true)).toBe(true);
  });

  it("drops events from an old backend generation", () => {
    expect(acceptsTerminalGeneration("gen-current", "gen-current")).toBe(true);
    expect(acceptsTerminalGeneration("gen-current", "gen-old")).toBe(false);
    expect(acceptsTerminalGeneration("gen-current", undefined)).toBe(true);
  });
});
