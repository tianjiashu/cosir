import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { cancelToolCall } from "@/lib/assistant/cancel-tool-call";

describe("cancelToolCall", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("posts to the run-scoped tool cancellation endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(cancelToolCall(12, "call/1")).resolves.toEqual({ kind: "signalled" });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/runs\/12\/tool-calls\/call%2F1\/cancel$/),
      expect.objectContaining({
        method: "POST",
        cache: "no-store",
        headers: expect.objectContaining({ "X-Trace-Id": expect.any(String) }),
      }),
    );
  });

  it("reports an existing cancellation signal separately from a new signal", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 409 })));

    await expect(cancelToolCall(12, "call-1")).resolves.toEqual({ kind: "already_signalled" });
  });

  it("reports a missing run and network failure without claiming cancellation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    await expect(cancelToolCall(12, "call-1")).resolves.toMatchObject({ kind: "failed", reason: "run_not_found" });

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await expect(cancelToolCall(12, "call-1")).resolves.toMatchObject({ kind: "failed", reason: "network" });
  });

  it("rejects invalid tool targets without making an HTTP request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(cancelToolCall(null, "call-1")).resolves.toMatchObject({ kind: "failed", reason: "invalid_target" });
    await expect(cancelToolCall(12, "   ")).resolves.toMatchObject({ kind: "failed", reason: "invalid_target" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
