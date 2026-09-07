import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { cancelRun } from "@/lib/assistant/cancel-run";

describe("cancelRun", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("reports HTTP 409 as not cancellable instead of success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 409 })));
    const result = await cancelRun(7, 12);
    expect(result).toEqual(expect.objectContaining({ accepted: false, reason: "not_cancellable" }));
  });

  it("reports an accepted cancellation only for a successful response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 202 })));
    await expect(cancelRun(7, 12)).resolves.toEqual({ accepted: true });
  });
});
