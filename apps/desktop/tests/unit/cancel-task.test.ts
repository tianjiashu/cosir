import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { cancelTask } from "@/lib/assistant/cancel-task";

describe("cancelTask", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("reports a non-2xx response as rejected instead of success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    const result = await cancelTask(7);
    expect(result).toEqual(expect.objectContaining({ accepted: false, reason: "rejected" }));
  });

  it("posts to the task-scoped cancel endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(cancelTask(7)).resolves.toEqual({ accepted: true });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/tasks/7/cancel");
  });
});
