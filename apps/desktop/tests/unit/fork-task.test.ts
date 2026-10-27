import { describe, expect, it, vi } from "vitest";

const postJson = vi.hoisted(() => vi.fn(async () => ({ task_id: 44 })));
vi.mock("@/lib/http/client", () => ({ postJson, requestJson: vi.fn() }));

import { forkTask } from "@/lib/api/workspaces";

describe("forkTask（E3）", () => {
  it.each(["7", "assistant-7", "assistant-run-3"])("按消息切分：请求体只带消息 id %s，不带 runId", async (messageId) => {
    await forkTask(42, messageId);
    expect(postJson).toHaveBeenLastCalledWith("/tasks/42/fork", { messageId });
  });
});
