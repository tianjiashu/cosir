import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/http/client", () => ({
  requestJson: vi.fn(),
}));

import { requestJson } from "@/lib/http/client";
import { requestAssistantSnapshot } from "@/lib/assistant/assistant-snapshot-client";
import type { TransportState } from "@/lib/assistant/contract";

const requestJsonMock = vi.mocked(requestJson);

function validSnapshot(): TransportState {
  return {
    runs: [],
    current_run_id: null,
    approvals: {},
    context_usage_ratio: null,
    context_usage_used: null,
    context_window_total: null,
    error: null,
  };
}

describe("assistant snapshot client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("统一请求地址、trace 和 snapshot schema 校验", async () => {
    const snapshot = validSnapshot();
    const controller = new AbortController();
    requestJsonMock.mockResolvedValue(snapshot);

    await expect(requestAssistantSnapshot(42, {
      signal: controller.signal,
      traceId: "trace-42",
    })).resolves.toBe(snapshot);

    expect(requestJsonMock).toHaveBeenCalledWith("/tasks/42/assistant/state", {
      signal: controller.signal,
      traceId: "trace-42",
    });
  });

  it("拒绝非法 snapshot，而不是把原始响应交给状态机", async () => {
    requestJsonMock.mockResolvedValue({ ...validSnapshot(), unexpected: true });

    await expect(requestAssistantSnapshot(42)).rejects.toThrow("snapshot");
  });

  it("保留非 2xx 错误给调用方处理", async () => {
    const error = new Error("HTTP 409");
    requestJsonMock.mockRejectedValue(error);

    await expect(requestAssistantSnapshot(42)).rejects.toBe(error);
  });

  it("保留 AbortError 给调用方处理", async () => {
    const error = new DOMException("request aborted", "AbortError");
    requestJsonMock.mockRejectedValue(error);

    await expect(requestAssistantSnapshot(42)).rejects.toBe(error);
  });
});
