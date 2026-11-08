import { describe, expect, it, vi } from "vitest";

const postJson = vi.hoisted(() => vi.fn(async () => ({ task_id: 44 })));
vi.mock("@/lib/http/client", () => ({ postJson, requestJson: vi.fn() }));

import { forkTask } from "@/lib/api/workspaces";

describe("forkTask（E3）", () => {
  it("按回复分叉：请求体只带回复 id", async () => {
    await forkTask(42, "assistant-turn-9");
    expect(postJson).toHaveBeenLastCalledWith("/tasks/42/fork", { replyMessageId: "assistant-turn-9" });
  });
});
