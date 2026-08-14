// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { TurnTimeline } from "@/components/layout/TurnTimeline";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";
import { useDelegationStore } from "@/stores/delegationStore";
import { useEventStore } from "@/stores/eventStore";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/services/backend", () => ({
  openFileInEditor: () => undefined,
}));

function event(
  eventId: string,
  eventType: RuntimeEvent["event_type"],
  turnId: string,
  payload: Record<string, unknown>,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: "task_1",
    turn_id: turnId,
    sequence,
    created_at: `2026-08-10T00:00:0${sequence}Z`,
    payload,
  } as RuntimeEvent;
}

describe("TurnTimeline 并发组泳道左边框", () => {
  const parentTurn = {
    turn_id: "turn_parent",
    task_id: "task_1",
    input_text: "delegate two",
    status: "running",
    response_text: null,
    end_reason: null,
    created_at: "2026-08-10T00:00:00Z",
    updated_at: "2026-08-10T00:00:01Z",
  } as TurnRecord;

  it("并发组 delegation 行外层 div 带并发组左边框 class", () => {
    const parentEvents = [
      event(
        "e1",
        "delegation_child_started",
        "turn_parent",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "running",
        },
        1,
      ),
      event(
        "e2",
        "delegation_child_started",
        "turn_parent",
        {
          delegation_id: "del_b",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_b",
          child_agent_id: "delegate_analyst",
          delegation_type: "analysis",
          status: "running",
        },
        2,
      ),
    ];

    const { container } = render(<TurnTimeline turn={parentTurn} events={parentEvents} />);

    // 同一并发组的两条 delegation 行外层 div 应带 border-l-2 border-primary/40 泳道左边框。
    const laneDivs = Array.from(container.querySelectorAll("div.border-l-2")).filter((el) =>
      el.className.includes("border-primary/40"),
    );
    expect(laneDivs.length).toBe(2);
  });

  it("非并发 delegation 行外层 div 不带并发组左边框 class", () => {
    const parentEvents = [
      event(
        "e1",
        "delegation_child_started",
        "turn_parent",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "running",
        },
        1,
      ),
    ];

    const { container } = render(<TurnTimeline turn={parentTurn} events={parentEvents} />);

    const laneDivs = Array.from(container.querySelectorAll("div.border-l-2")).filter((el) =>
      el.className.includes("border-primary/40"),
    );
    expect(laneDivs.length).toBe(0);
  });
});

describe("SubagentPanel 并发 sibling tab 切换", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
    useEventStore.getState().clearEvents();
  });

  it("注入两个 sibling delegation 事件，选中其一，渲染 2 个 tab 且点击另一 tab 切换", () => {
    useEventStore.getState().setEvents(
      [
        event(
          "e1",
          "delegation_child_started",
          "turn_parent",
          {
            delegation_id: "del_a",
            parent_turn_id: "turn_parent",
            child_turn_id: "turn_child_a",
            child_agent_id: "delegate_reviewer",
            delegation_type: "review",
            status: "running",
          },
          1,
        ),
        event(
          "e2",
          "delegation_child_started",
          "turn_parent",
          {
            delegation_id: "del_b",
            parent_turn_id: "turn_parent",
            child_turn_id: "turn_child_b",
            child_agent_id: "delegate_analyst",
            delegation_type: "analysis",
            status: "running",
          },
          2,
        ),
      ],
      "task_1",
    );
    useDelegationStore.getState().selectChildTurn("turn_child_a");

    render(<SubagentPanel />);

    // 并发组（2 sibling）应渲染 2 个 tab（role=tab）。
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(2);

    // 当前选中项（turn_child_a）aria-current 标记。
    const currentTab = screen.getByRole("tab", { current: true });
    expect(currentTab.textContent).toMatch(/delegate_reviewer/);

    // 点击另一个 tab（delegate_analyst / turn_child_b）切换选中态。
    const otherTab = tabs.find((tab) => tab.textContent?.match(/delegate_analyst/))!;
    fireEvent.click(otherTab);

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_b");
  });

  it("非并发（单 child）时不渲染 tab 行", () => {
    useEventStore.getState().setEvents(
      [
        event(
          "e1",
          "delegation_child_started",
          "turn_parent",
          {
            delegation_id: "del_a",
            parent_turn_id: "turn_parent",
            child_turn_id: "turn_child_a",
            child_agent_id: "delegate_reviewer",
            delegation_type: "review",
            status: "running",
          },
          1,
        ),
      ],
      "task_1",
    );
    useDelegationStore.getState().selectChildTurn("turn_child_a");

    render(<SubagentPanel />);

    expect(screen.queryAllByRole("tab")).toHaveLength(0);
  });
});
