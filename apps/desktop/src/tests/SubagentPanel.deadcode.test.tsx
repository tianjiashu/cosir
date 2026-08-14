// @vitest-environment happy-dom
/**
 * 修复 3 回归测试：删除 SubagentPanel 中未使用的 projectTurnTimeline 死代码调用。
 *
 * 验证：
 *  1. 渲染契约不变：child 事件到达后，child turn 元信息头部 + TurnTimeline 区域渲染，
 *     且不再显示 "正在等待子 Agent 事件流…" loading。
 *  2. 空态正确：选中 childEvents 为空的 child → 显示 loading（新哨兵 childEvents.length > 0 行为正确）。
 *  3. 未选中空态：selectedChildTurnId 为 null → 显示提示空态文案。
 *  4. 无引用悬空：渲染过程不抛 ReferenceError（projectTurnTimeline / childTimelineItem 等死代码已删除干净、import 干净），
 *     通过「成功渲染 + 无 console.error 捕获」间接验证。
 *
 * 不修改任何业务代码，仅 seed store 后渲染断言。
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";
import { useDelegationStore } from "@/stores/delegationStore";
import { useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";

/** 构造测试用 RuntimeEvent（与既有测试一致的极简工厂，不依赖真实后端）。 */
function runtimeEvent(
  eventId: string,
  eventType: RuntimeEvent["event_type"],
  turnId: string,
  payload: Record<string, unknown>,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: "task-deadcode",
    turn_id: turnId,
    sequence,
    created_at: `2026-08-11T00:00:${String(sequence).padStart(2, "0")}Z`,
    payload,
  } as RuntimeEvent;
}

/** 构造最小 TurnRecord（真实落库，避免触发兜底 synthetic record）。 */
function turnRecord(turnId: string, status: "pending" | "running" | "completed" = "running") {
  return {
    turn_id: turnId,
    task_id: "task-deadcode",
    input_text: `input-${turnId}`,
    status,
    end_reason: null,
    response_text: null,
    created_at: "",
    updated_at: "",
  };
}

const TASK_ID = "task-deadcode";
const CHILD_WITH_EVENTS = "turn-child-with-events";
const CHILD_EMPTY = "turn-child-empty";

// TurnTimeline stub：仅渲染一个 data-testid 标记以验证 child 分片被传入渲染，
// 不走真实投影（与 SubagentPanel.deferred.test 约定一致）。
vi.mock("@/components/layout/TurnTimeline", () => ({
  TurnTimeline: ({ events }: { turn: unknown; events: RuntimeEvent[] }) =>
    events.length === 0 ? null : (
      <div data-testid="turn-timeline" data-len={String(events.length)}>
        timeline:{events.length}
      </div>
    ),
}));

describe("修复 3 回归：SubagentPanel 删除 projectTurnTimeline 死代码", () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;
  let consoleWarnSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
    useEventStore.getState().clearEvents();
    useTurnStore.getState().setTurnsForTask(TASK_ID, []);
    // 捕获渲染期可能抛出的 ReferenceError 痕迹（死代码引用悬空会触发 console.error / 抛错）。
    consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    consoleWarnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    consoleErrorSpy.mockRestore();
    consoleWarnSpy.mockRestore();
  });

  // 测试目的：验证删除死代码后，含 child 事件的场景仍正常渲染 child timeline（渲染契约不变）。
  // 可能发现的缺陷：误删渲染组件引用 / 新哨兵逻辑写错导致已到事件却显示 loading。
  it("渲染契约不变：选中含 child 事件的 turn 时，渲染元信息头部 + TurnTimeline，且不显示 loading", async () => {
    const childEvents = [
      runtimeEvent("dc-shard-0", "model_output_delta", CHILD_WITH_EVENTS, { text: "chunk-0" }, 1000),
      runtimeEvent("dc-shard-1", "model_output_delta", CHILD_WITH_EVENTS, { text: "chunk-1" }, 1001),
    ];

    useEventStore.setState({
      eventsByTurnId: { [CHILD_WITH_EVENTS]: childEvents },
      events: childEvents,
    });
    useTurnStore.setState({ turnsByTaskId: { [TASK_ID]: [turnRecord(CHILD_WITH_EVENTS)] } });
    useDelegationStore.getState().selectChildTurn(CHILD_WITH_EVENTS);

    render(<SubagentPanel />);

    // 元信息头部包含 child turn id。
    expect(screen.getByText(CHILD_WITH_EVENTS)).toBeTruthy();
    // TurnTimeline 区域被渲染（传入非空分片）。
    const timeline = screen.getByTestId("turn-timeline");
    expect(timeline.getAttribute("data-len")).toBe("2");
    // 已到事件，不应显示 loading 占位。
    expect(screen.queryByText("正在等待子 Agent 事件流…")).toBeNull();
    // 未选中态的提示文案不应出现。
    expect(screen.queryByText("点击对话中的委派行以查看子 Agent 时间线")).toBeNull();
    // 渲染过程无 console.error（间接证明无引用悬空 / 无异常）。
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  // 测试目的：验证新哨兵 childEvents.length > 0 对「空 child 事件」的判定正确（显示 loading）。
  // 可能发现的缺陷：哨兵写成 childTimelineItem 残留引用导致 ReferenceError，或空态不显示 loading。
  it("空态正确：选中 childEvents 为空的 child，显示 loading 占位", () => {
    // 选中一个在 eventStore 中没有任何分片事件的 child turn。
    useEventStore.setState({ eventsByTurnId: {}, events: [] });
    useTurnStore.setState({ turnsByTaskId: { [TASK_ID]: [turnRecord(CHILD_EMPTY)] } });
    useDelegationStore.getState().selectChildTurn(CHILD_EMPTY);

    render(<SubagentPanel />);

    // child turn 元信息头部仍渲染（selectedChildTurnId 可见）。
    expect(screen.getByText(CHILD_EMPTY)).toBeTruthy();
    // child 事件未到 → 显示 loading 占位（新哨兵 childEvents.length > 0 为 false）。
    expect(screen.getByText("正在等待子 Agent 事件流…")).toBeTruthy();
    // 不应渲染 TurnTimeline（无分片）。
    expect(screen.queryByTestId("turn-timeline")).toBeNull();
    // 无引用悬空导致的 console.error。
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  // 测试目的：验证 selectedChildTurnId 为 null 时回到未选中提示空态（无功能回归）。
  // 可能发现的缺陷：删除死代码扰动未选中分支，导致空态文案丢失或抛错。
  it("未选中空态：selectedChildTurnId 为 null 时显示提示空态", () => {
    useEventStore.setState({ eventsByTurnId: {}, events: [] });
    useDelegationStore.getState().clearSelection();

    render(<SubagentPanel />);

    expect(screen.getByText("点击对话中的委派行以查看子 Agent 时间线")).toBeTruthy();
    // 不应出现 child 相关渲染（无选中）。
    expect(screen.queryByTestId("turn-timeline")).toBeNull();
    expect(screen.queryByText("正在等待子 Agent 事件流…")).toBeNull();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  // 测试目的：验证「兜底 synthetic turn record」路径下（turnStore 无真实记录），
  // 删除死代码后仍能正常渲染含 child 事件的 timeline（无引用悬空）。
  // 可能发现的缺陷：死代码删除不彻底，在兜底分支仍引用已移除的 projectTurnTimeline / childTimelineItem。
  it("无引用悬空：兜底 synthetic record 路径下，含事件仍能渲染且无 ReferenceError", () => {
    const childEvents = [
      runtimeEvent("dc-fb-0", "model_output_delta", CHILD_WITH_EVENTS, { text: "a" }, 2000),
    ];
    // 故意不在 turnStore 落库，触发 createFallbackTurn 兜底路径。
    useEventStore.setState({
      eventsByTurnId: { [CHILD_WITH_EVENTS]: childEvents },
      events: childEvents,
    });
    useTurnStore.setState({ turnsByTaskId: { [TASK_ID]: [] } });
    useDelegationStore.getState().selectChildTurn(CHILD_WITH_EVENTS);

    // 渲染本身不应抛异常（若 projectTurnTimeline/childTimelineItem 仍被引用会抛 ReferenceError）。
    expect(() => render(<SubagentPanel />)).not.toThrow();

    expect(screen.getByText(CHILD_WITH_EVENTS)).toBeTruthy();
    expect(screen.getByTestId("turn-timeline")).toBeTruthy();
    expect(screen.queryByText("正在等待子 Agent 事件流…")).toBeNull();
    // 任何 ReferenceError 都会经 console.error 暴露，断言其未被调用。
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });
});
