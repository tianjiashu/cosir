import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  type TurnTimelineEntry,
} from "@/services/timeline/projector";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

function delegationEvent(
  eventId: string,
  eventType: RuntimeEvent["event_type"],
  payload: Record<string, unknown>,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: "task_1",
    turn_id: "turn_parent",
    sequence,
    created_at: `2026-08-10T00:00:0${sequence}Z`,
    payload,
  } as RuntimeEvent;
}

function delegationEntries(entries: TurnTimelineEntry[]) {
  return entries.filter(
    (entry): entry is Extract<TurnTimelineEntry, { kind: "delegation" }> =>
      entry.kind === "delegation",
  );
}

describe("timeline delegation projection", () => {
  it("projects delegation lifecycle as one stable entry", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_started",
        {
          delegation_id: "del_1",
          parent_turn_id: "turn_parent",
          child_turn_id: "",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "pending",
        },
        1,
      ),
      delegationEvent(
        "e2",
        "delegation_finished",
        {
          delegation_id: "del_1",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "completed",
          summary: "ok",
        },
        2,
      ),
    ]);

    const delegations = delegationEntries(state.entries);

    expect(delegations).toHaveLength(1);
    expect(delegations[0].item).toMatchObject({
      delegationId: "del_1",
      childAgentId: "delegate_reviewer",
      childTurnId: "turn_child",
      delegationType: "review",
      status: "completed",
      summary: "ok",
    });
  });

  it("updates the existing delegation entry when child start arrives later", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_started",
        {
          delegation_id: "del_2",
          parent_turn_id: "turn_parent",
          child_turn_id: "",
          child_agent_id: "delegate_analyst",
          delegation_type: "analysis",
          status: "pending",
        },
        1,
      ),
    ]);
    const firstEntry = delegationEntries(state.entries)[0];

    state = projectTimelineIncrementally(state, [
      delegationEvent(
        "e2",
        "delegation_child_started",
        {
          delegation_id: "del_2",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_2",
          child_agent_id: "delegate_analyst",
          delegation_type: "analysis",
          status: "running",
        },
        2,
      ),
    ]);

    const delegations = delegationEntries(state.entries);

    expect(delegations).toHaveLength(1);
    expect(delegations[0]).not.toBe(firstEntry);
    expect(delegations[0].item.status).toBe("running");
    expect(delegations[0].item.childTurnId).toBe("turn_child_2");
  });

  it("projects failed and cancelled lifecycle details without creating duplicates", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_started",
        {
          delegation_id: "del_3",
          parent_turn_id: "turn_parent",
          child_turn_id: "",
          child_agent_id: "delegate_coder",
          delegation_type: "coding",
          status: "pending",
        },
        1,
      ),
      delegationEvent(
        "e2",
        "delegation_failed",
        {
          delegation_id: "del_3",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_3",
          child_agent_id: "delegate_coder",
          delegation_type: "coding",
          status: "failed",
          error: "child tool approval denied",
        },
        2,
      ),
      delegationEvent(
        "e3",
        "delegation_cancelled",
        {
          delegation_id: "del_3",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_3",
          child_agent_id: "delegate_coder",
          delegation_type: "coding",
          status: "cancelled",
          error: "parent turn cancelled",
        },
        3,
      ),
    ]);

    const delegations = delegationEntries(state.entries);

    expect(delegations).toHaveLength(1);
    expect(delegations[0].item.status).toBe("cancelled");
    expect(delegations[0].item.error).toBe("parent turn cancelled");
  });

  it("returns the previous state when repeated event ids are replayed", () => {
    const event = delegationEvent(
      "e1",
      "delegation_started",
      {
        delegation_id: "del_4",
        parent_turn_id: "turn_parent",
        child_agent_id: "delegate_reviewer",
        delegation_type: "review",
        status: "pending",
      },
      1,
    );
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [event]);
    const replayed = projectTimelineIncrementally(state, [event]);

    expect(replayed).toBe(state);
  });

  it("falls back to event type status for unknown payload status", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_finished",
        {
          delegation_id: "del_5",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_5",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "future_status",
          summary: "ok",
        },
        1,
      ),
    ]);

    expect(delegationEntries(state.entries)[0].item.status).toBe("completed");
  });

  it("skips malformed delegation events without delegation id", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_started",
        {
          parent_turn_id: "turn_parent",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "pending",
        },
        1,
      ),
      delegationEvent(
        "e2",
        "delegation_started",
        {
          parent_turn_id: "turn_parent",
          child_agent_id: "delegate_analyst",
          delegation_type: "analysis",
          status: "pending",
        },
        2,
      ),
    ]);

    expect(delegationEntries(state.entries)).toHaveLength(0);
  });

  it("does not downgrade a terminal delegation when nonterminal event arrives later", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_finished",
        {
          delegation_id: "del_6",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_6",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "completed",
          summary: "ok",
        },
        1,
      ),
      delegationEvent(
        "e2",
        "delegation_child_started",
        {
          delegation_id: "del_6",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_6",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "running",
        },
        2,
      ),
    ]);

    const delegation = delegationEntries(state.entries)[0].item;
    expect(delegation.status).toBe("completed");
    expect(delegation.summary).toBe("ok");
  });

  it("preserves existing non-delegation entry references when adding a delegation", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent("assistant-1", "final_response", { step_id: "step_1", status: "completed", text: "done" }, 1),
    ]);
    const assistantEntry = state.entries[0];

    state = projectTimelineIncrementally(state, [
      delegationEvent(
        "e2",
        "delegation_started",
        {
          delegation_id: "del_7",
          parent_turn_id: "turn_parent",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "pending",
        },
        2,
      ),
    ]);

    expect(state.entries[0]).toBe(assistantEntry);
    expect(delegationEntries(state.entries)).toHaveLength(1);
  });
});
