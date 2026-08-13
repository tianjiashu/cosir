// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { DelegationTimelineEntry } from "@/components/chat/DelegationTimelineEntry";
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

describe("B2 DelegationTimelineEntry 并发文案边界", () => {
  it("传入 concurrencyGroupSize=3, concurrencyIndex=2 时渲染「并发 3/3」文案", () => {
    // 目的：验证并发序号从 0 起，index=2 时展示 3/3，覆盖非 1/2 的一般并发规模。
    // 可能暴露的缺陷：index 未 +1、size 拼接错误、未处理 >=3 的并发组。
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child_c"
        delegationType="review"
        status="running"
        concurrencyGroupSize={3}
        concurrencyIndex={2}
      />,
    );

    expect(screen.getByText("并发 3 / 3")).toBeTruthy();
  });

  it("concurrencyGroupSize=1（非并发）时不渲染并发文案（undefined 路径）", () => {
    // 目的：验证 size=1 被显式排除（brief：非并发不渲染并发指示），覆盖 undefined 以外的 false 分支。
    // 可能暴露的缺陷：size>=2 判断写成 >0，导致单委托也误显示并发徽章。
    render(
      <DelegationTimelineEntry
        childAgentId="delegate_reviewer"
        childTurnId="turn_child"
        delegationType="review"
        status="running"
        concurrencyGroupSize={1}
        concurrencyIndex={0}
      />,
    );

    expect(screen.queryByText(/并发/)).toBeNull();
  });
});

describe("B2 TurnTimeline 并发组泳道容器 class", () => {
  const parentTurn = {
    turn_id: "turn_parent",
    task_id: "task_1",
    input_text: "delegate",
    status: "running",
    response_text: null,
    end_reason: null,
    created_at: "2026-08-10T00:00:00Z",
    updated_at: "2026-08-10T00:00:01Z",
  } as TurnRecord;

  it("非并发 delegation 行外层容器 div 不带并发组左边框（border-l-2/border-primary）", () => {
    // 目的：独立验证 TurnTimeline 在非并发下不加 lane class，避免误把普通委托行归组。
    // 可能暴露的缺陷：lane 判断条件反向或阈值错误，导致单委托也加粗左边框。
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

  it("并发组（size>=2）delegation 行外层容器 div 精确实例化 border-l-2 与 border-primary/40", () => {
    // 目的：精确断言并发 lane class 两个 token 同时存在（防止只加了一个 token 的回归）。
    // 可能暴露的缺陷：只加了 border-l-2 漏加 border-primary/40，或反之。
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

    const laneDivs = Array.from(container.querySelectorAll("div.border-l-2")).filter(
      (el) => el.className.includes("border-l-2") && el.className.includes("border-primary/40"),
    );
    expect(laneDivs.length).toBe(2);
  });
});

describe("B3 SubagentPanel 并发 tab 切换（3 sibling）", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
    useEventStore.getState().clearEvents();
  });

  function siblingEvents() {
    return [
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
      event(
        "e3",
        "delegation_child_started",
        "turn_parent",
        {
          delegation_id: "del_c",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_c",
          child_agent_id: "delegate_planner",
          delegation_type: "plan",
          status: "running",
        },
        3,
      ),
    ];
  }

  it("注入 3 个 sibling 事件，选中 A，渲染 3 个 tab 且 aria-current 在 A", () => {
    // 目的：覆盖 >2 sibling 的 tab 渲染与当前项标记，验证长度判定仍正确。
    // 可能暴露的缺陷：tab 行仅在恰好 2 时渲染、aria-current 错位、索引越界。
    useEventStore.getState().setEvents(siblingEvents(), "task_1");
    useDelegationStore.getState().selectChildTurn("turn_child_a");

    render(<SubagentPanel />);

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(3);

    const currentTab = screen.getByRole("tab", { current: true });
    expect(currentTab.textContent).toMatch(/delegate_reviewer/);
  });

  it("点击第三个 sibling 的 tab 触发 selectChildTurn 切到该 child", () => {
    // 目的：验证非首项 sibling 的点击也能正确切到对应 childTurnId（覆盖索引 2 的切换路径）。
    // 可能暴露的缺陷：onClick 误用索引/首项 childTurnId，导致切到错误 child。
    useEventStore.getState().setEvents(siblingEvents(), "task_1");
    useDelegationStore.getState().selectChildTurn("turn_child_a");

    render(<SubagentPanel />);

    const tabs = screen.getAllByRole("tab");
    const thirdTab = tabs.find((tab) => tab.textContent?.match(/delegate_planner/))!;
    expect(thirdTab).toBeTruthy();
    fireEvent.click(thirdTab);

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_c");
  });
});
