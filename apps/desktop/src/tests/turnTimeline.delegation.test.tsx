// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
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

describe("TurnTimeline delegation child entries", () => {
  it("renders expanded child turn events from the child event provider", () => {
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
    const childEvents = [
      event(
        "e2",
        "final_response",
        "turn_child",
        { step_id: "step_1", status: "completed", text: "child review details" },
        2,
      ),
    ];

    render(
      <TurnTimeline
        turn={parentTurn}
        events={parentEvents}
        getChildEvents={(childTurnId) => (childTurnId === "turn_child" ? childEvents : [])}
      />,
    );

    expect(screen.queryByText("child review details")).toBeNull();

    fireEvent.click(screen.getByLabelText("Expand delegated child events"));

    expect(screen.getByText("child review details")).toBeTruthy();
  });

  it("refreshes child entries when only child event revision changes", () => {
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
    let childEvents: RuntimeEvent[] = [];
    const getChildEvents = (childTurnId: string) => (childTurnId === "turn_child" ? childEvents : []);
    const { rerender } = render(
      <TurnTimeline
        turn={parentTurn}
        events={parentEvents}
        getChildEvents={getChildEvents}
        childEventsRevision=""
      />,
    );

    expect(screen.queryByLabelText("Expand delegated child events")).toBeNull();

    childEvents = [
      event(
        "e2",
        "final_response",
        "turn_child",
        { step_id: "step_1", status: "completed", text: "late child details" },
        2,
      ),
    ];
    rerender(
      <TurnTimeline
        turn={parentTurn}
        events={parentEvents}
        getChildEvents={getChildEvents}
        childEventsRevision="turn_child:1:e2"
      />,
    );

    fireEvent.click(screen.getByLabelText("Expand delegated child events"));

    expect(screen.getByText("late child details")).toBeTruthy();
  });
});
