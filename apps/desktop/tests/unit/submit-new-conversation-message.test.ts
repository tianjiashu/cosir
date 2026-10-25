import { afterEach, describe, expect, it, vi } from "vitest";

import {
  submitNewConversationMessage,
  wasCommandPossiblyAccepted,
  type SubmitNewConversationMessageInput,
} from "@/lib/assistant/submit-new-conversation-message";
import { HttpError } from "@/lib/http/errors";

const input: SubmitNewConversationMessageInput = {
  taskId: 7,
  workspaceId: 3,
  text: "hello",
  imageAttachments: [],
  banTools: [],
  modelConfigId: 1,
  reasoningEffort: "high",
  traceId: "0".repeat(32),
};

afterEach(() => vi.unstubAllGlobals());

describe("submitNewConversationMessage", () => {
  it("以 202 投递作为后端已受理的边界，发送到 Task 消息端点", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 202 })));

    await expect(submitNewConversationMessage(input)).resolves.toBeUndefined();

    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/tasks\/7\/messages$/);
    expect(JSON.parse(String(init.body))).toMatchObject({
      message: { role: "user", parts: [{ type: "text", text: "hello" }] },
      modelConfigId: 1,
      delivery: "immediate",
    });
  });

  it("treats a non-2xx response as a definitive rejection with the backend reason", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { error: { code: "INVALID_MESSAGE", message: "测试拒绝", retryable: true } } }),
      { status: 400, headers: { "Content-Type": "application/json" } },
    )));

    const error = await submitNewConversationMessage(input).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).message).toBe("测试拒绝");
    expect(wasCommandPossiblyAccepted(error)).toBe(false);
  });

  it("treats a network-level failure as an unknown delivery outcome", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = await submitNewConversationMessage(input).catch((cause: unknown) => cause);
    expect(wasCommandPossiblyAccepted(error)).toBe(true);
  });
});
