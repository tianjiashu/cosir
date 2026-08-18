// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { TurnTimeline } from "@/components/layout/TurnTimeline";

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

describe("TurnTimeline delegation rendering", () => {
  it("renders delegation title only (no inline expand affordance) and preserves terminal text", () => {
    const parentTurn = {
      turn_id: "turn_parent",
      task_id: "task_1",
      input_text: "delegate review",
      status: "completed",
      response_text: null,
      end_reason: null,
      created_at: "2026-08-10T00:00:00Z",
      updated_at: "2026-08-10T00:00:01Z",
    } as TurnRecord;
    const parentEvents = [
      event(
        "e1",
        "delegation_finished",
        "turn_parent",
        {
          delegation_id: "del_1",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "completed",
          summary: "review completed",
        },
        1,
      ),
    ];

    render(<TurnTimeline turn={parentTurn} events={parentEvents} />);

    // 内联展开 DOM 已移除：折叠箭头不存在，child 流只在 Subagent Tab 渲染。
    expect(screen.queryByLabelText("Expand delegated child events")).toBeNull();
    // 终态文案保留在行内。
    // 注意：summary 经 markdown 渲染后 `review` 与 `completed` 被拆到不同 <span>，
    // 不能用 getByText 精确匹配整段，也不能假设二者在 textContent 中相邻；
    // 改断言整段 body 文本中两个终态 token 都已渲染出来。
    const bodyText = document.body.textContent ?? "";
    expect(bodyText).toContain("review");
    expect(bodyText).toContain("completed");
    // 子 Agent 标识与状态徽章保留。
    expect(screen.getByText("delegate_reviewer")).toBeTruthy();
    expect(screen.getByText("completed")).toBeTruthy();
  });
});
