// @vitest-environment happy-dom
/**
 * useCopyToClipboard（§5）**边界与失败路径可排查性**补充测试。
 *
 * 现有 `useCopyToClipboard.test.ts` 覆盖了成功置位 / 2 秒复位 / 失败不抛，
 * 本文件补充：失败路径必须经统一日志出口 `logWarn`（携带 module 上下文）记录，
 * 而非裸 `console.warn`（否则失败不落盘 `logs/desktop.log`，线上不可排查）；
 * 以及定时器卸载清理、连续复制重置、返回值契约、navigator.clipboard 缺失不崩。
 */

// logger 需在被测模块导入前置 mock，才能拦截 hook 内部的日志调用。
vi.mock("@/lib/logger", () => ({
  logDebug: vi.fn(),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));

import { renderHook, act } from "@testing-library/react";
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard";
import { logWarn } from "@/lib/logger";

/** 安装一个可控的 clipboard mock。 */
function installClipboard(writeText: () => Promise<void>): void {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
}

describe("useCopyToClipboard 失败路径日志可排查性（§5）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // 复制失败必须经统一日志出口 logWarn 记录，而非裸 console.warn。
  it("复制失败经 logWarn 记录（而非裸 console.warn）", async () => {
    installClipboard(() => Promise.reject(new Error("denied")));
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy("x");
    });
    expect(logWarn).toHaveBeenCalled();
  });

  // 失败日志必须携带 module 上下文，保证能定位到来源模块。
  it("失败日志携带 module 上下文", async () => {
    installClipboard(() => Promise.reject(new Error("denied")));
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy("x");
    });
    const call = (logWarn as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(call, "logWarn 未被调用，无法校验上下文").toBeDefined();
    const ctx = call[1] as Record<string, unknown> | undefined;
    expect(ctx, "失败日志缺少上下文参数").toBeDefined();
    expect(ctx?.module).toBe("useCopyToClipboard");
  });
});

describe("useCopyToClipboard 状态与定时器边界（§5）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    installClipboard(() => Promise.resolve());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("复制成功 copied 置位且 2 秒后复位", async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      const ok = await result.current.copy("hello");
      expect(ok).toBe(true);
    });
    expect(result.current.copied).toBe(true);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.copied).toBe(false);
  });

  it("连续复制重置未触发的复位定时器", async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy("a");
    });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.copied).toBe(true);
    await act(async () => {
      await result.current.copy("b");
    });
    // 第二次复制重置了定时器，需再等满 2 秒才复位
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.copied).toBe(true);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.copied).toBe(false);
  });

  it("卸载后推进定时器不产生 React 状态更新告警", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { result, unmount } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy("a");
    });
    unmount();
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(errSpy).not.toHaveBeenCalled();
    errSpy.mockRestore();
  });
});

describe("copy 返回值契约（§5 + ToolCallCard）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // hook 内部已 catch 并 resolve(false)，调用方无需再 .catch 记录（避免死代码）。
  it("失败时 copy 返回 false（调用方 .catch 不可达）", async () => {
    installClipboard(() => Promise.reject(new Error("denied")));
    const { result } = renderHook(() => useCopyToClipboard());
    let ret = true;
    await act(async () => {
      ret = await result.current.copy("x");
    });
    // 返回 false 即证明 hook 内部已吞掉异常并 resolve，调用方写的 .catch 永不触发（死代码）。
    expect(ret).toBe(false);
  });

  it("失败后 copied 保持 false（无假成功反馈）", async () => {
    installClipboard(() => Promise.reject(new Error("denied")));
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy("x");
    });
    expect(result.current.copied).toBe(false);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.copied).toBe(false);
  });

  it("navigator.clipboard 不存在时不抛异常且返回 false、有日志", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const { result } = renderHook(() => useCopyToClipboard());
    let ret = true;
    await act(async () => {
      ret = await result.current.copy("x");
    });
    expect(ret).toBe(false);
    expect(result.current.copied).toBe(false);
    expect(logWarn).toHaveBeenCalled();
  });
});
