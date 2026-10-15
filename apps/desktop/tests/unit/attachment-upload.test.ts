import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { uploadAttachment } from "@/lib/assistant/attachments/attachment-upload";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";

/** 只实现 uploadAttachment 用到的 XHR 表面，由测试驱动完成 / 超时 / 失败。 */
class FakeXhr {
  static last: FakeXhr | null = null;
  headers: Record<string, string> = {};
  status = 0;
  response: unknown = null;
  responseType = "";
  timeout = 0;
  upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;
  constructor() { FakeXhr.last = this; }
  open() {}
  setRequestHeader(name: string, value: string) { this.headers[name] = value; }
  send() {}
  abort() { this.onabort?.(); }
}

const file = () => new File(["x"], "a.png", { type: "image/png" });

describe("uploadAttachment", () => {
  beforeEach(() => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    vi.stubGlobal("FormData", class { append() {} });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("sets a finite timeout and the caller's trace id", () => {
    void uploadAttachment(7, file(), undefined, { traceId: "trace-1" });
    expect(FakeXhr.last?.timeout).toBeGreaterThan(0);
    expect(FakeXhr.last?.headers["X-Trace-Id"]).toBe("trace-1");
  });

  // 卡住的上传必须以失败结束，否则 composer 的发送状态永远不释放。
  it("rejects with a TimeoutError when the backend never answers", async () => {
    const pending = uploadAttachment(7, file());
    FakeXhr.last?.ontimeout?.();
    const error = await pending.catch((cause: unknown) => cause);
    expect(error).toMatchObject({ name: "TimeoutError" });
    expect(userErrorMessage(error, "fallback")).toContain("超时");
  });

  it("turns a non-2xx response into an HttpError carrying the backend reason", async () => {
    const pending = uploadAttachment(7, file());
    const xhr = FakeXhr.last!;
    xhr.status = 413;
    xhr.response = { detail: { message: "图片超过 20MB 上限" } };
    xhr.onload?.();
    const error = await pending.catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect(userErrorMessage(error, "fallback")).toBe("图片超过 20MB 上限");
  });

  it("aborts when the caller cancels", async () => {
    const controller = new AbortController();
    const pending = uploadAttachment(7, file(), undefined, { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("resolves the uploaded attachment on success", async () => {
    const pending = uploadAttachment(7, file());
    const xhr = FakeXhr.last!;
    xhr.status = 201;
    xhr.response = { id: "img-1", name: "a.png", status: "ready" };
    xhr.onload?.();
    await expect(pending).resolves.toMatchObject({ id: "img-1" });
  });
});
