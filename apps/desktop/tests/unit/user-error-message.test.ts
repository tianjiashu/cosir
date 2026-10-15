import { describe, expect, it } from "vitest";

import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/errors";

const FALLBACK = "保存失败，请检查配置后重试";

describe("userErrorMessage", () => {
  // 后端给出的可行动原因必须透出，否则用户只看到「请重试」，无从知道要改什么。
  it("shows the backend's readable reason", () => {
    expect(userErrorMessage(new HttpError("模型配置名称已存在", { status: 409 }), FALLBACK)).toBe("模型配置名称已存在");
  });

  it("translates framework default status phrases instead of leaking English", () => {
    expect(userErrorMessage(new HttpError("Not Found", { status: 404 }), FALLBACK)).toBe("请求的内容不存在或已被删除");
    expect(userErrorMessage(new HttpError("HTTP request failed with status 500", { status: 500 }), FALLBACK))
      .toBe("本机后端处理失败，请查看后端日志");
  });

  it("shows intentional frontend messages verbatim", () => {
    expect(userErrorMessage(new UserFacingError("请先选择模型"), FALLBACK)).toBe("请先选择模型");
  });

  it("maps network, timeout and cancellation failures to fixed copy", () => {
    expect(userErrorMessage(new TypeError("Failed to fetch"), FALLBACK)).toContain("无法连接本机后端");
    expect(userErrorMessage(new TypeError("Load failed"), FALLBACK)).toContain("无法连接本机后端");
    expect(userErrorMessage(new DOMException("x", "TimeoutError"), FALLBACK)).toContain("超时");
    expect(userErrorMessage(new DOMException("x", "AbortError"), FALLBACK)).toBe("请求已取消");
  });

  // 程序缺陷不能被误报为「连不上后端」，也不能把英文原文展示给用户。
  it("falls back for programming errors and unknown values", () => {
    expect(userErrorMessage(new TypeError("Cannot read properties of undefined"), FALLBACK)).toBe(FALLBACK);
    expect(userErrorMessage(new Error("socket hang up"), FALLBACK)).toBe(FALLBACK);
    expect(userErrorMessage(undefined, FALLBACK)).toBe(FALLBACK);
  });
});
