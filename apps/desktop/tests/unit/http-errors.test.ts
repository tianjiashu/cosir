import { describe, expect, it } from "vitest";

import { HttpError, httpErrorMessage, parseHttpError } from "@/lib/http/errors";

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

describe("httpErrorMessage", () => {
  // 目的：后端给出的可行动原因必须透出到界面。潜在缺陷：吞掉原因后用户只看到「请重试」，
  // 无从知道要改什么（例如 409 的配置名称重复）。
  it("透出后端 HTTP 错误原因", () => {
    const error = new HttpError("模型配置名称已存在", { status: 409 });

    expect(httpErrorMessage(error, "保存失败，请检查配置后重试")).toBe("模型配置名称已存在");
  });

  // 目的：用户无法据此纠正的失败（网络中断、无正文）回退调用方兜底文案。
  it("缺少可读原因时回退兜底文案", () => {
    expect(httpErrorMessage(new Error("socket hang up"), "保存失败，请检查配置后重试"))
      .toBe("保存失败，请检查配置后重试");
    expect(httpErrorMessage(new HttpError("", { status: 500 }), "保存失败，请检查配置后重试"))
      .toBe("保存失败，请检查配置后重试");
    expect(httpErrorMessage(undefined, "保存失败，请检查配置后重试"))
      .toBe("保存失败，请检查配置后重试");
  });
});
