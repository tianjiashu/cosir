import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { cancelTask } from "@/lib/assistant/cancel-task";

describe("cancelTask", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("reports HTTP 409 as not cancellable instead of success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 409 })));
    const result = await cancelTask(7);
    expect(result).toEqual(expect.objectContaining({ accepted: false, reason: "not_cancellable" }));
  });

  it("posts to the task-scoped cancel endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(cancelTask(7)).resolves.toEqual({ accepted: true });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/tasks/7/cancel");
  });

  it("rejects a missing task id without a request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(cancelTask(null)).resolves.toEqual(expect.objectContaining({ reason: "invalid_task_id" }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
