import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import { useRuntimeCancellation } from "@/components/assistant/runtime/use-runtime-cancellation";
import type { TransportState } from "@/lib/assistant/contract";
import { activity, transportState } from "./transport-fixtures";

function setup(state: TransportState) {
  const context = {
    latestStateRef: { current: state },
    cancelRequestedRef: { current: false },
    setIssue: vi.fn(),
  } as unknown as RuntimeSessionContext;
  let cancellation!: ReturnType<typeof useRuntimeCancellation>;
  function Probe() {
    cancellation = useRuntimeCancellation(context);
    return null;
  }
  renderToStaticMarkup(createElement(Probe));
  return { context, cancellation };
}

describe("Task 级停止标记（D7）", () => {
  it("请求停止后标记保持到活动结束，不比对 runId", () => {
    const { context, cancellation } = setup(transportState({ activity: activity(1, "running") }));
    cancellation.onRequested();
    expect(context.cancelRequestedRef.current).toBe(true);

    cancellation.onResult(true);
    expect(context.cancelRequestedRef.current).toBe(true);

    // 新一轮已开始（runId 变化）也不影响：只看 Task 是否仍有活动。
    cancellation.onStateCommitted(transportState({ activity: activity(2, "running") }));
    expect(context.cancelRequestedRef.current).toBe(true);
    cancellation.onSettled();
    expect(context.cancelRequestedRef.current).toBe(false);
  });

  it("停止被拒或信号到达时活动已结束，立即清除标记", () => {
    const rejected = setup(transportState({ activity: activity(1, "running") }));
    rejected.cancellation.onRequested();
    rejected.cancellation.onResult(false);
    expect(rejected.context.cancelRequestedRef.current).toBe(false);

    const finished = setup(transportState({ activity: activity(1, "completed") }));
    finished.cancellation.onRequested();
    finished.cancellation.onResult(true);
    expect(finished.context.cancelRequestedRef.current).toBe(false);
  });

  it("提交的 state 已没有活动时清除标记", () => {
    const { context, cancellation } = setup(transportState({ activity: activity(1, "suspended") }));
    cancellation.onRequested();
    cancellation.onStateCommitted(transportState({ activity: activity(1, "cancelled") }));
    expect(context.cancelRequestedRef.current).toBe(false);
  });
});
