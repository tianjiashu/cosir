/**
 * 回归测试：客户端断开（client_disconnected）事件现在被正确处理。
 *
 * 修复前：projector.ts 终态白名单仅 run_finished/run_failed/run_cancelled，裸
 * client_disconnected 被静默忽略，UI 永远停在 running/active。
 *
 * 修复后：
 *   - projector 将 client_disconnected 纳入终态白名单，投影为 kind:"status" 终态条目。
 *   - useSSE.runtimeStatusFromEvent 识别 client_disconnected（裸事件与 run_failed 兜底），
 *     透传 end_reason="client_disconnected" 且标为终态 failed。
 *   - StatusBadge 对 client_disconnected 给出差异化文案（区别于普通失败）。
 *
 * @module tests/review.clientDisconnectedNoUI
 */

import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));

import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  type TurnTimelineEntry,
} from "@/services/timeline/projector";
import { runtimeStatusFromEvent } from "@/hooks/useSSE";
import { StatusBadge } from "@/components/chat/StatusBadge";

function makeEvent(eventType: string, payload: Record<string, unknown>) {
  return {
    event_id: `evt-${Math.random().toString(36).slice(2)}`,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as unknown as Parameters<typeof projectTimelineIncrementally>[1][number];
}

function statusEntry(state: ReturnType<typeof projectTimelineIncrementally>) {
  return state.entries.find((e): e is Extract<TurnTimelineEntry, { kind: "status" }> => e.kind === "status");
}

describe("client_disconnected 现在被正确处理（回归）", () => {
  it("裸 client_disconnected 事件被投影为终态 status 条目（不再被忽略）", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("run_started", { step_id: "step-1" }),
      makeEvent("client_disconnected", { step_id: "step-1" }),
    ]);
    const entry = statusEntry(state);
    expect(entry).toBeDefined();
    expect(entry!.eventType).toBe("client_disconnected");
  });

  it("对照：run_cancelled 仍被正确投影为终态", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("run_started", { step_id: "step-1" }),
      makeEvent("run_cancelled", { step_id: "step-1" }),
    ]);
    expect(statusEntry(state)!.eventType).toBe("run_cancelled");
  });

  it("run_failed + end_reason=client_disconnected 透传差异化 end_reason（区别于普通失败）", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("run_started", { step_id: "step-1" }),
      makeEvent("run_failed", { error: "client disconnected", end_reason: "client_disconnected", step_id: "step-1" }),
    ]);
    const entry = statusEntry(state);
    expect(entry!.eventType).toBe("run_failed");
    expect((entry!.payload as { end_reason?: string }).end_reason).toBe("client_disconnected");
  });

  it("useSSE.runtimeStatusFromEvent 识别裸 client_disconnected 为终态 failed（核心 UI 映射）", () => {
    const status = runtimeStatusFromEvent(makeEvent("client_disconnected", { step_id: "step-1" }) as never);
    expect(status).not.toBeNull();
    expect(status!.terminal).toBe(true);
    expect(status!.taskStatus).toBe("failed");
    expect(status!.turnStatus).toBe("failed");
    expect(status!.endReason).toBe("client_disconnected");
  });

  it("useSSE.runtimeStatusFromEvent 对 run_failed+client_disconnected 透传 end_reason（非普通 error 文案）", () => {
    const status = runtimeStatusFromEvent(
      makeEvent("run_failed", { error: "boom", end_reason: "client_disconnected", step_id: "step-1" }) as never,
    );
    expect(status!.endReason).toBe("client_disconnected");
  });

  it("StatusBadge 对 run_failed+client_disconnected 渲染差异化提示文案（区别于普通失败）", () => {
    const props = { eventType: "run_failed" as const, payload: { error: "x", end_reason: "client_disconnected", step_id: "step-1" } };
    const { container } = render(<StatusBadge {...props} />);
    const text = container.textContent ?? "";
    expect(text).toContain("连接已中断");
    expect(text).toContain("重新连接后重试");
  });
});
