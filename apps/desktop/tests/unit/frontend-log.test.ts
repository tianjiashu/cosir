import { afterEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ write: vi.fn<(entry: unknown) => Promise<void>>(async () => undefined) }));

vi.mock("@/lib/desktop-host", () => ({ desktopHost: { log: { write: host.write } } }));

import { frontendLog } from "@/lib/logging/frontend-log";

afterEach(() => {
  host.write.mockReset();
  host.write.mockImplementation(async () => undefined);
  vi.restoreAllMocks();
});

// 目的：日志是诊断旁路，契约是永不 reject。潜在缺陷：只包住宿主写入时，构造日志条目的步骤
// （异常值转文本）抛错会让 frontendLog reject——`await` 它的错误路径（如对话历史加载失败）因此
// 跳过后续的错误提示，`void` 调用则留下未处理的 Promise rejection。
describe("frontendLog", () => {
  const exoticValues: Array<[string, unknown]> = [
    ["null 原型对象", Object.create(null)],
    ["toString 会抛错的对象", { toString() { throw new Error("boom"); } }],
    ["message 不是字符串的 Error", Object.assign(new Error("x"), { message: 42 })],
  ];

  it.each(exoticValues)("异常值为 %s 时仍写出一条降级日志且不 reject", async (_label, value) => {
    await expect(frontendLog("ERROR", "test_event", "测试", { error: value })).resolves.toBeUndefined();

    expect(host.write).toHaveBeenCalledTimes(1);
    expect(host.write.mock.calls[0][0]).toMatchObject({ event: "test_event", error: { message: "异常值无法转成文本" } });
  });

  it("宿主写入失败时不 reject，只在控制台警告", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    host.write.mockRejectedValueOnce(new Error("write failed"));

    await expect(frontendLog("INFO", "test_event", "测试")).resolves.toBeUndefined();

    expect(warn).toHaveBeenCalledWith("[cosir.frontend] frontend_log_failed", expect.any(Error));
  });
});
