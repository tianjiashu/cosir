import { describe, expect, it } from "vitest";

import { HttpError, parseHttpError } from "@/lib/http/errors";

describe("parseHttpError", () => {
  it("parses a direct structured error", async () => {
    const error = await parseHttpError(new Response(
      JSON.stringify({ error: { code: "MODEL_REQUIRED", message: "请选择模型", retryable: false } }),
      { status: 400 },
    ));

    expect(error).toBeInstanceOf(HttpError);
    expect(error).toMatchObject({
      status: 400,
      code: "MODEL_REQUIRED",
      message: "请选择模型",
      retryable: false,
    });
  });

  it("parses FastAPI detail-wrapped structured errors", async () => {
    const error = await parseHttpError(new Response(
      JSON.stringify({ detail: { error: { code: "TASK_MISMATCH", message: "任务不匹配", retryable: false } } }),
      { status: 409 },
    ));

    expect(error).toMatchObject({
      status: 409,
      code: "TASK_MISMATCH",
      message: "任务不匹配",
      retryable: false,
    });
  });

  it("does not expose an unstructured response body", async () => {
    const error = await parseHttpError(new Response("backend stack trace", { status: 500 }));

    expect(error).toMatchObject({
      status: 500,
      message: "HTTP request failed with status 500",
    });
  });
});
