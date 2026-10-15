import { describe, expect, it, vi } from "vitest";

import {
  submitAssistantTransport,
  wasCommandPossiblyAccepted,
  type SubmitAssistantTransportInput,
} from "@/lib/assistant/submit-assistant-transport";
import { HttpError } from "@/lib/http/errors";

const input: SubmitAssistantTransportInput = {
  taskId: 7,
  workspaceId: 3,
  commandId: "creation-1",
  text: "hello",
  imageAttachments: [],
  banTools: [],
  modelConfigId: 1,
  reasoningEffort: "high",
  traceId: "0".repeat(32),
};

describe("submitAssistantTransport", () => {
  it("returns after the 2xx accepted barrier without waiting for SSE completion", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", {
      status: 200,
      headers: { "X-Cosir-Task-Id": "7" },
    })));

    await expect(submitAssistantTransport({
      taskId: 7,
      workspaceId: 3,
      commandId: "creation-1",
      text: "hello",
      imageAttachments: [],
      banTools: [],
      modelConfigId: 1,
      reasoningEffort: "high",
      traceId: "0".repeat(32),
    })).resolves.toBeUndefined();

    expect(fetch).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  it("treats a non-2xx response as a definitive rejection with the backend reason", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ error: { code: "TASK_BUSY", message: "测试拒绝", retryable: true } }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));

    const error = await submitAssistantTransport(input).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).message).toBe("测试拒绝");
    expect(wasCommandPossiblyAccepted(error)).toBe(false);
    vi.unstubAllGlobals();
  });

  it("treats a network-level failure as an unknown delivery outcome", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = await submitAssistantTransport(input).catch((cause: unknown) => cause);
    expect(wasCommandPossiblyAccepted(error)).toBe(true);
    vi.unstubAllGlobals();
  });
});
