import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
vi.mock("@/lib/backend/runtime-config", () => ({
  getBackendRuntimeSnapshot: () => ({ backendBaseUrl: "http://127.0.0.1:5100" }),
}));

import { requestJson, requestOk } from "@/lib/http/client";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("HTTP timeout ownership", () => {
  it("releases the header timeout and owner listener after success", async () => {
    const owner = new AbortController();
    const remove = vi.spyOn(owner.signal, "removeEventListener");
    let signal: AbortSignal;
    vi.stubGlobal("fetch", vi.fn(async (_url, init: RequestInit) => {
      signal = init.signal!;
      return new Response("ok");
    }));
    const response = await requestOk("/test", { signal: owner.signal, timeoutMs: 100 });
    expect(vi.getTimerCount()).toBe(0);
    expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
    owner.abort();
    await vi.advanceTimersByTimeAsync(200);
    expect(signal!.aborted).toBe(false);
    expect(await response.text()).toBe("ok");
  });

  it("releases the timeout after a network failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network failure")));
    await expect(requestOk("/test", { timeoutMs: 100 })).rejects.toThrow("network failure");
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["owner", "timeout"])("preserves %s cancellation and releases resources", async (reason) => {
    const owner = new AbortController();
    vi.stubGlobal("fetch", vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal!.addEventListener("abort", () => reject(init.signal!.reason), { once: true });
    })));
    const pending = requestOk("/test", { signal: owner.signal, timeoutMs: 100 });
    const result = expect(pending).rejects.toMatchObject({ name: reason === "owner" ? "AbortError" : "TimeoutError" });
    if (reason === "owner") owner.abort();
    else await vi.advanceTimersByTimeAsync(100);
    await result;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps a JSON timeout until the response body is consumed", async () => {
    let finish!: (value: unknown) => void;
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: () => new Promise((resolve) => { finish = resolve; }) })));
    const pending = requestJson("/test", { timeoutMs: 100 });
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    finish({ done: true });
    await expect(pending).resolves.toEqual({ done: true });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("releases the header timeout while a non-2xx body is still being read", async () => {
    let finish!: (body: string) => void;
    let signal!: AbortSignal;
    vi.stubGlobal("fetch", vi.fn(async (_url, init: RequestInit) => {
      signal = init.signal!;
      return { ok: false, status: 409, text: () => new Promise<string>((resolve) => { finish = resolve; }) };
    }));
    const pending = requestOk("/test", { timeoutMs: 100 });
    const result = expect(pending).rejects.toMatchObject({ name: "HttpError", status: 409, message: "conflict" });
    await vi.advanceTimersByTimeAsync(200);
    expect(vi.getTimerCount()).toBe(0);
    expect(signal.aborted).toBe(false);
    finish(JSON.stringify({ detail: "conflict" }));
    await result;
  });
});
