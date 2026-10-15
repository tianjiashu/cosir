import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { classifyBackendTransition } from "@/lib/backend/backend-transition";

const backend = (generation: number, available: boolean) => ({
  backendBaseUrl: available ? `http://127.0.0.1:${4000 + generation}` : "http://127.0.0.1:0",
  generation,
  available,
});

describe("classifyBackendTransition", () => {
  it("reports unavailable once when the backend goes away", () => {
    expect(classifyBackendTransition(backend(1, true), backend(1, false))).toEqual({ kind: "unavailable" });
    expect(classifyBackendTransition(backend(1, false), backend(1, false))).toBeNull();
  });

  it("reports restored when the same instance becomes available again", () => {
    expect(classifyBackendTransition(backend(1, false), backend(1, true))).toEqual({
      kind: "restored",
      previousGeneration: 1,
      generation: 1,
      wasUnavailable: true,
    });
  });

  it("reports restored when the supervisor swaps in a new instance without an unavailable gap", () => {
    expect(classifyBackendTransition(backend(1, true), backend(2, true))).toEqual({
      kind: "restored",
      previousGeneration: 1,
      generation: 2,
      wasUnavailable: false,
    });
  });

  it("ignores snapshots that change nothing observable", () => {
    expect(classifyBackendTransition(backend(3, true), backend(3, true))).toBeNull();
  });
});
