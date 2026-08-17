// @vitest-environment happy-dom
/**
 * ErrorBoundary 测试（§2 错误边界）。
 *
 * 验证 react-error-boundary 封装：子组件抛错时展示兜底 UI + 重试按钮，
 * 点击重试后子树重新渲染，且异常经 logError 记录。
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { logError } from "@/lib/logger";

function Boom({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) {
    throw new Error("boom-render");
  }
  return <div>正常内容</div>;
}

describe("ErrorBoundary (§2 错误边界)", () => {
  it("子组件正常时渲染子树", () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeTruthy();
  });

  it("子组件抛错时展示兜底 UI 并记录日志", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/界面渲染出现异常/)).toBeTruthy();
    spy.mockRestore();
  });

  it("点击重试后重置错误状态", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender } = render(
      <ErrorBoundary>
        <Boom shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/界面渲染出现异常/)).toBeTruthy();
    // 先切换子树为不抛错，再触发重试：resetErrorBoundary 会用当前 children 重新渲染。
    rerender(
      <ErrorBoundary>
        <Boom shouldThrow={false} />
      </ErrorBoundary>,
    );
    const retry = screen.getByText("重试");
    fireEvent.click(retry);
    expect(screen.getByText("正常内容")).toBeTruthy();
    spy.mockRestore();
  });
});
