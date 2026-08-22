// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { deriveInputAvailability, useInputState } from "@/components/layout/useInputState";

/**
 * A. deriveInputAvailability 纯函数单测。
 * 覆盖 canSend / canStop 各布尔因子 + 组合边界。
 */
describe("deriveInputAvailability", () => {
  const base = {
    hasDraftText: true,
    isLoading: false,
    isStreaming: false,
    isSubmitting: false,
    hasContext: true,
    hasModelSelected: true,
  };

  it("全部满足 → canSend=true, canStop=false", () => {
    const r = deriveInputAvailability(base);
    expect(r.canSend).toBe(true);
    expect(r.canStop).toBe(false);
  });

  it("hasDraftText=false → canSend=false（空草稿不可发送）", () => {
    expect(deriveInputAvailability({ ...base, hasDraftText: false }).canSend).toBe(false);
  });

  it("isLoading=true → canSend=false（加载中不可发送）", () => {
    expect(deriveInputAvailability({ ...base, isLoading: true }).canSend).toBe(false);
  });

  it("isStreaming=true → canSend=false（流式中不可发送）", () => {
    expect(deriveInputAvailability({ ...base, isStreaming: true }).canSend).toBe(false);
  });

  it("isSubmitting=true → canSend=false（提交中不可发送）", () => {
    expect(deriveInputAvailability({ ...base, isSubmitting: true }).canSend).toBe(false);
  });

  it("hasContext=false → canSend=false（无上下文不可发送）", () => {
    expect(deriveInputAvailability({ ...base, hasContext: false }).canSend).toBe(false);
  });

  it("hasModelSelected=false → canSend=false（未选模型不可发送）", () => {
    expect(deriveInputAvailability({ ...base, hasModelSelected: false }).canSend).toBe(false);
  });

  it("canStop: 流式中且非加载 → true", () => {
    expect(deriveInputAvailability({ ...base, isStreaming: true, isLoading: false }).canStop).toBe(true);
  });

  it("canStop: 流式中但加载中 → false", () => {
    expect(deriveInputAvailability({ ...base, isStreaming: true, isLoading: true }).canStop).toBe(false);
  });

  it("canStop: 非流式 → false（即便未加载）", () => {
    expect(deriveInputAvailability({ ...base, isStreaming: false, isLoading: false }).canStop).toBe(false);
    expect(deriveInputAvailability({ ...base, isStreaming: false, isLoading: true }).canStop).toBe(false);
  });

  it("边界：单一因子翻转不应影响另一因子（canSend=false 时仍可 canStop）", () => {
    const r = deriveInputAvailability({ ...base, isStreaming: true, isLoading: false });
    expect(r.canSend).toBe(false);
    expect(r.canStop).toBe(true);
  });
});

/**
 * B. useInputState 提交互斥锁（同步锁防连点）。
 */
describe("useInputState 提交互斥锁", () => {
  const input = {
    hasDraftText: true,
    isLoading: false,
    isStreaming: false,
    hasContext: true,
    hasModelSelected: true,
  };

  it("beginSubmit 第一次返回 true，未 endSubmit 时第二次返回 false", () => {
    const { result } = renderHook(() => useInputState(input));
    expect(result.current.isSubmitting).toBe(false);
    expect(result.current.beginSubmit()).toBe(true);
    expect(result.current.beginSubmit()).toBe(false);
  });

  it("beginSubmit 后 isSubmitting=true、canSend=false", () => {
    const { result } = renderHook(() => useInputState(input));
    act(() => {
      result.current.beginSubmit();
    });
    expect(result.current.isSubmitting).toBe(true);
    expect(result.current.canSend).toBe(false);
    expect(result.current.phase).toBe("submitting");
  });

  it("endSubmit 后 beginSubmit 再次返回 true，状态恢复", () => {
    const { result } = renderHook(() => useInputState(input));
    act(() => {
      expect(result.current.beginSubmit()).toBe(true);
    });
    expect(result.current.isSubmitting).toBe(true);
    act(() => {
      result.current.endSubmit();
    });
    expect(result.current.isSubmitting).toBe(false);
    expect(result.current.phase).toBe("idle");
    act(() => {
      expect(result.current.beginSubmit()).toBe(true);
    });
    expect(result.current.isSubmitting).toBe(true);
  });

  it("派生可用性随输入因子变化（isLoading=true 时 canSend=false）", () => {
    const { result, rerender } = renderHook(
      (props: typeof input) => useInputState(props),
      { initialProps: input },
    );
    expect(result.current.canSend).toBe(true);
    rerender({ ...input, isLoading: true });
    expect(result.current.canSend).toBe(false);
  });

  it("边界：仅 beginSubmit 不 endSubmit 时 isSubmitting 保持 true（暴露必须配对）", () => {
    const { result } = renderHook(() => useInputState(input));
    act(() => {
      result.current.beginSubmit();
    });
    expect(result.current.isSubmitting).toBe(true);
  });
});
