import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { uploadAttachment } from "@/lib/assistant/attachments/attachment-upload";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";

const file = () => new File(["x"], "a.png", { type: "image/png" });

function stubFetch(implementation: (url: string, init: RequestInit) => Promise<Response>) {
  const fetchMock = vi.fn(implementation);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("uploadAttachment", () => {
  // http client 按 runtime config 解析后端地址，它读 window；单测跑在 Node 环境，给一个空 window。
  beforeEach(() => vi.stubGlobal("window", {}));
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("posts the file as multipart form data with the caller's trace id", async () => {
    const fetchMock = stubFetch(async () => Response.json({ id: "img-1" }, { status: 201 }));
    await uploadAttachment(7, file(), "trace-1");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toMatch(/\/workspaces\/7\/attachments$/);
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Record<string, string>)["X-Trace-Id"]).toBe("trace-1");
  });

  // 卡住的上传必须以失败结束，否则 composer 的发送状态永远不释放。
  it("rejects with a TimeoutError when the backend never answers", async () => {
    vi.useFakeTimers();
    stubFetch((_url, init) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
    }));
    const pending = uploadAttachment(7, file()).catch((cause: unknown) => cause);
    await vi.runAllTimersAsync();
    const error = await pending;
    expect(error).toMatchObject({ name: "TimeoutError" });
    expect(userErrorMessage(error, "fallback")).toContain("超时");
  });

  it("turns a non-2xx response into an HttpError carrying the backend reason", async () => {
    stubFetch(async () => Response.json({ detail: { message: "图片超过 20MB 上限" } }, { status: 413 }));
    const error = await uploadAttachment(7, file()).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect(userErrorMessage(error, "fallback")).toBe("图片超过 20MB 上限");
  });

  it("resolves the uploaded attachment on success", async () => {
    stubFetch(async () => Response.json({ id: "img-1", name: "a.png", status: "ready" }, { status: 201 }));
    await expect(uploadAttachment(7, file())).resolves.toMatchObject({ id: "img-1" });
  });
});
