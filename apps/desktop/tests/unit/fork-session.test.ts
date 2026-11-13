import { describe, expect, it, vi } from "vitest";

const postJson = vi.hoisted(() => vi.fn(async () => ({ session_id: 44 })));
vi.mock("@/lib/http/client", () => ({ postJson, requestJson: vi.fn() }));

import { forkSession } from "@/lib/api/workspaces";

describe("forkSession（E3）", () => {
  it("按回复分叉：请求体只带回复 id", async () => {
    await forkSession(42, "assistant-turn-9");
    expect(postJson).toHaveBeenLastCalledWith("/sessions/42/fork", { replyMessageId: "assistant-turn-9" });
  });
});
