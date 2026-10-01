import { describe, expect, it, vi } from "vitest";

import { submitAssistantTransport } from "@/lib/assistant/submit-assistant-transport";

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
      allowsTools: [],
      modelConfigId: 1,
      reasoningEffort: "high",
    })).resolves.toBeUndefined();

    expect(fetch).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });
});
