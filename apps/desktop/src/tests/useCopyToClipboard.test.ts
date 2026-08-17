// @vitest-environment happy-dom
/**
 * useCopyToClipboard Hook 测试（§5 剪贴板复制）。
 *
 * 验证复制成功时 `copied` 置位、调用 navigator.clipboard.writeText；
 * 复制失败时记录告警且不抛出；2 秒后复位（用 fake timers 验证）。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
// 在导入被测模块前 mock logger，才能拦截 hook 内部的 logWarn 调用。
vi.mock("@/lib/logger", () => ({
  logDebug: vi.fn(),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard";
import { logWarn } from "@/lib/logger";

describe("useCopyToClipboard (§5 剪贴板复制)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("复制成功时 copied 置位并调用 clipboard.writeText", async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy("hello");
    });
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("hello");
    expect(result.current.copied).toBe(true);
  });

  it("2 秒后 copied 复位", async () => {
    const { result } = renderHook(() => useCopyToClipboard());
    await act(async () => {
      await result.current.copy("hello");
    });
    expect(result.current.copied).toBe(true);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.copied).toBe(false);
  });

  it("复制失败时记录告警且不抛出", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
      configurable: true,
    });
    const { result } = renderHook(() => useCopyToClipboard());
    let ret = true;
    await act(async () => {
      ret = await result.current.copy("x");
    });
    expect(ret).toBe(false);
    expect(logWarn).toHaveBeenCalled();
    expect(result.current.copied).toBe(false);
  });
});
