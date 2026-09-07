import { describe, expect, it } from "vitest";

import { parseTransportError } from "@/lib/assistant/transport-error";

describe("parseTransportError", () => {
  it("recovers the backend transport error from assistant-ui's Error wrapper", () => {
    const error = new Error('Status 400: {"error":{"code":"MODEL_SELECTION_REQUIRED","message":"请先选择模型","retryable":false}}');
    expect(parseTransportError(error)).toEqual({
      code: "MODEL_SELECTION_REQUIRED",
      message: "请先选择模型",
      retryable: false,
    });
  });

  it("recovers FastAPI HTTPException detail.error responses", () => {
    const error = new Error('Status 400: {"detail":{"error":{"code":"MODEL_SELECTION_REQUIRED","message":"请先选择模型和模型提供商","retryable":false}}}');
    expect(parseTransportError(error)).toEqual({
      code: "MODEL_SELECTION_REQUIRED",
      message: "请先选择模型和模型提供商",
      retryable: false,
    });
  });

  it("does not expose unstructured response bodies", () => {
    expect(parseTransportError(new Error("Status 500: backend stack trace"))).toBeNull();
    expect(parseTransportError(new Error("network failed"))).toBeNull();
  });
});
