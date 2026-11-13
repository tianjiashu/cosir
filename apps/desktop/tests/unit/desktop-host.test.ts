import { afterEach, describe, expect, it, vi } from "vitest";

import type { CosirHost } from "@/electron/shared/host-contract";

/**
 * 宿主控制面的失败消息由宿主保证是给用户看的中文（`electron/main/ipc.ts`）；渲染侧唯一出口把它转成用户可见错误。
 * 潜在缺陷：宿主写的原因（「只能打开网页或本机文件」「标签页数量已达上限」）到界面上全变成「请稍后重试」。
 */

async function load(host: Partial<CosirHost>) {
  vi.resetModules();
  vi.stubGlobal("window", { cosirHost: host });
  return (await import("@/lib/desktop-host")).desktopHost!;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("desktopHost", () => {
  it("turns a host failure into an error whose reason the interface shows", async () => {
    const host = await load({ browser: { close: () => Promise.reject(new Error("标签页数量已达上限，请先关掉一些")) } as never });
    const failure = await host.browser.close("t1").catch((error: unknown) => error);
    // 重置模块后错误类是新的一份：同一轮里再取文案入口。
    const { userErrorMessage } = await import("@/lib/errors/user-error-message");
    expect(userErrorMessage(failure, "请稍后重试")).toBe("标签页数量已达上限，请先关掉一些");
  });

  it("passes results and synchronous values through unchanged", async () => {
    const unsubscribe = () => undefined;
    const host = await load({
      platform: "darwin",
      backend: { onChange: () => unsubscribe, snapshot: () => Promise.resolve({ generation: 3 }) } as never,
    });
    expect(host.platform).toBe("darwin");
    expect(host.backend.onChange(() => undefined)).toBe(unsubscribe);
    await expect(host.backend.snapshot()).resolves.toEqual({ generation: 3 });
  });
});
