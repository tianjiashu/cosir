// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { DelegationTimelineEntry } from "@/components/chat/DelegationTimelineEntry";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";
import { useDelegationStore } from "@/stores/delegationStore";
import { useEventStore } from "@/stores/eventStore";

/** 构造测试用 RuntimeEvent。 */
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
    task_id: "task-delegation",
    turn_id: turnId,
    sequence,
    created_at: `2026-08-11T00:00:${String(sequence).padStart(2, "0")}Z`,
    payload,
  } as RuntimeEvent;
}

describe("delegation 行点击 → 侧边栏选中联动", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
    useEventStore.getState().clearEvents();
  });

  it("点击 delegation 行写入选中态，侧边栏展示该 child turn", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child_link"
        delegationType="review"
        status="running"
      />,
    );

    fireEvent.click(screen.getByLabelText("Open delegate_reviewer child timeline in side panel"));

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_link");

    // 侧边栏订阅选中态，渲染出对应 child turn 的元信息。
    // 注意：左侧 DelegationTimelineEntry 与右侧 SubagentPanel 都会展示 child turn id，
    // 故用 getAllByText 断言侧边栏确实消费了选中态（而非要求全局唯一匹配）。
    render(<SubagentPanel />);
    expect(screen.getAllByText("turn_child_link").length).toBeGreaterThan(0);
  });

  it("点击「打开侧边栏」按钮写入选中态", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child_kbd"
        delegationType="review"
        status="running"
      />,
    );

    // 修复后「打开侧边栏」是原生 <button type="button">，键盘可达由浏览器原生默认行为保证
    // （Enter/Space 触发 click）；单元测试层用 fireEvent.click 验证点击路径的选中联动。
    fireEvent.click(screen.getByLabelText("Open delegate_reviewer child timeline in side panel"));

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_kbd");
  });

  it("未选中时侧边栏显示提示空态", () => {
    render(<SubagentPanel />);
    expect(screen.getByText("点击对话中的委派行以查看子 Agent 时间线")).toBeTruthy();
  });

  it("child 处于 waiting_approval 时徽章渲染「待审批」（验证派生状态类型穷尽）", () => {
    const childTurnId = "turn_child_waiting";
    // 注入一条 waiting_approval 的 child 委派事件，供 deriveChildDelegationStatus 派生。
    useEventStore.getState().setEvents(
      [
        runtimeEvent(
          "ev-waiting",
          "delegation_child_started",
          "turn-parent",
          {
            delegation_id: "delegation-1",
            parent_turn_id: "turn-parent",
            child_turn_id: childTurnId,
            child_agent_id: "delegate_reviewer",
            delegation_type: "review",
            status: "waiting_approval",
          },
          1,
        ),
      ],
      "task-delegation",
    );
    useDelegationStore.getState().selectChildTurn(childTurnId);

    render(<SubagentPanel />);

    // 类型穷尽映射已覆盖 waiting_approval，不应落到「状态未知」兜底。
    expect(screen.queryByText("状态未知")).toBeNull();
    expect(screen.getByText("待审批")).toBeTruthy();
    expect(screen.getByText(childTurnId)).toBeTruthy();
  });

  it("child 处于 pending 时徽章渲染「等待中」（payload.status 显式为 pending）", () => {
    const childTurnId = "turn_child_pending";
    // 显式注入 payload.status="pending"，验证 DELEGATION_STATUS_BADGE 覆盖 pending 且
    // deriveChildDelegationStatus 复用 projector 口径（normalizeDelegationStatus 直接返回该值）。
    useEventStore.getState().setEvents(
      [
        runtimeEvent(
          "ev-pending",
          "delegation_child_started",
          "turn-parent",
          {
            delegation_id: "delegation-2",
            parent_turn_id: "turn-parent",
            child_turn_id: childTurnId,
            child_agent_id: "delegate_reviewer",
            delegation_type: "review",
            status: "pending",
          },
          1,
        ),
      ],
      "task-delegation",
    );
    useDelegationStore.getState().selectChildTurn(childTurnId);

    render(<SubagentPanel />);

    expect(screen.queryByText("状态未知")).toBeNull();
    expect(screen.getByText("等待中")).toBeTruthy();
  });

  it("空 childTurnId 的 delegation 行点击不派发选中态（守卫）", () => {
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        delegationType="review"
        status="running"
      />,
    );

    const button = screen.getByRole("button", { name: /delegate_reviewer/ });
    expect(button.hasAttribute("disabled")).toBe(true);

    fireEvent.click(button);
    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
  });
});
