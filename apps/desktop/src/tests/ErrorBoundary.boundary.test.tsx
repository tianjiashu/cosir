// @vitest-environment happy-dom
/**
 * ErrorBoundary（§2）**日志可排查性与边界**补充测试。
 *
 * 现有 `ErrorBoundary.test.tsx` 虽然 import 了 `logError`，但**从未对其做任何断言**
 * （测试名声称「并记录日志」，实际只断言了兜底 UI 文案），属于弱覆盖：
 * 若 onError 回调被删除或改成空 catch，原测试仍会全绿。
 * 本文件通过 mock 统一日志出口，把「异常必须经 logError 记录且携带 module 上下文」
 * 这一可排查性契约显式断言出来。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// 必须在导入被测组件前 mock logger，才能拦截 onError/onReset 内部的日志调用。
vi.mock("@/lib/logger", () => ({
  logDebug: vi.fn(),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));

import { ErrorBoundary } from "@/components/ErrorBoundary";
import { logError, logInfo } from "@/lib/logger";

/** 受控抛错子组件。 */
function Boom({ shouldThrow, message = "boom-render" }: { shouldThrow: boolean; message?: string }) {
  if (shouldThrow) throw new Error(message);
  return <div>正常内容</div>;
}

describe("ErrorBoundary 异常日志可排查性（§2）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // React 会把捕获到的渲染异常额外打到 console.error，屏蔽噪音。
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // 测试目的：捕获的渲染异常必须经统一出口 logError 记录（不静默吞错）。
  // 可能发现的缺陷：onError 缺失或为空实现 → 白屏兜底但日志无痕，线上无法定位。
  it("捕获异常时调用 logError", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow />
      </ErrorBoundary>,
    );
    expect(logError).toHaveBeenCalledTimes(1);
  });

  // 测试目的：logError 必须收到原始 Error 对象（携带堆栈）与含 module 的上下文。
  // 可能发现的缺陷：只传消息字符串丢失堆栈，或漏传 module 导致无法定位边界位置。
  it("logError 收到原始 Error 与含 module 的上下文", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow message="specific-failure" />
      </ErrorBoundary>,
    );
    const call = (logError as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(call).toBeDefined();
    expect(typeof call[0]).toBe("string");
    expect(call[1]).toBeInstanceOf(Error);
    expect((call[1] as Error).message).toBe("specific-failure");
    const ctx = call[2] as Record<string, unknown>;
    expect(ctx).toBeDefined();
    expect(ctx.module).toBe("ErrorBoundary");
  });

  // 测试目的：正常渲染路径不得产生错误日志（避免污染错误日志、造成误报警）。
  // 可能发现的缺陷：无条件记录 error，日志噪音掩盖真实故障。
  it("子组件正常时不记录 logError", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(logError).not.toHaveBeenCalled();
    expect(screen.getByText("正常内容")).toBeTruthy();
  });

  // 测试目的：用户点击重试属正常操作，应记 INFO 而非 ERROR，且携带 module。
  // 可能发现的缺陷：重试记为 error 污染错误日志；或完全不记录导致复盘缺失重试动作。
  it("点击重试记录 logInfo（含 module）而非 logError", () => {
    const { rerender } = render(
      <ErrorBoundary>
        <Boom shouldThrow />
      </ErrorBoundary>,
    );
    const errorCallsBefore = (logError as unknown as { mock: { calls: unknown[][] } }).mock.calls
      .length;
    rerender(
      <ErrorBoundary>
        <Boom shouldThrow={false} />
      </ErrorBoundary>,
    );
    fireEvent.click(screen.getByText("重试"));
    expect(logInfo).toHaveBeenCalled();
    const infoCall = (logInfo as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect((infoCall[1] as Record<string, unknown>).module).toBe("ErrorBoundary");
    // 重试本身不应新增错误日志
    expect(
      (logError as unknown as { mock: { calls: unknown[][] } }).mock.calls.length,
    ).toBe(errorCallsBefore);
  });
});

describe("ErrorBoundary 兜底 UI 边界（§2）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // 测试目的：兜底 UI 应展示错误 message 便于用户/支持人员反馈定位。
  // 可能发现的缺陷：只显示固定文案，丢失差异化线索。
  it("兜底 UI 展示错误 message", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow message="磁盘写入失败" />
      </ErrorBoundary>,
    );
    expect(screen.getByText("磁盘写入失败")).toBeTruthy();
  });

  // 测试目的：错误 message 为空串时应回退「未知错误」，不得渲染空白区域。
  // 可能发现的缺陷：未做 falsy 兜底，用户看到无信息的空白提示。
  it("空 message 回退展示「未知错误」", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow message="" />
      </ErrorBoundary>,
    );
    expect(screen.getByText("未知错误")).toBeTruthy();
  });

  // 测试目的：兜底 UI 必须提供可点击的重试按钮（可恢复性契约）。
  // 可能发现的缺陷：退回无重试能力的 Class 版本，用户只能重启应用。
  it("兜底 UI 提供可点击的重试按钮", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow />
      </ErrorBoundary>,
    );
    const retry = screen.getByRole("button", { name: /重试/ });
    expect(retry).toBeTruthy();
    expect((retry as HTMLButtonElement).disabled).toBe(false);
  });

  // 测试目的：重试后子树仍抛错时应再次进入兜底并再次记录日志（不吞第二次错误）。
  // 可能发现的缺陷：重置后 onError 不再触发，反复失败无日志，形成静默死循环。
  it("重试后仍抛错则再次展示兜底并再次记录 logError", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow />
      </ErrorBoundary>,
    );
    expect(logError).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("重试"));
    expect(screen.getByText(/界面渲染出现异常/)).toBeTruthy();
    expect(
      (logError as unknown as { mock: { calls: unknown[][] } }).mock.calls.length,
    ).toBeGreaterThanOrEqual(2);
  });
});
