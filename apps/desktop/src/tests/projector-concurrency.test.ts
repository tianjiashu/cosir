import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  type TimelineDelegationItem,
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
  turnId: string = "turn_parent",
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

function delegationEntries(entries: TurnTimelineEntry[]) {
  return entries.filter(
    (entry): entry is Extract<TurnTimelineEntry, { kind: "delegation" }> =>
      entry.kind === "delegation",
  );
}

function delegationItems(entries: TurnTimelineEntry[]): TimelineDelegationItem[] {
  return delegationEntries(entries).map((entry) => entry.item);
}

describe("timeline delegation concurrency group", () => {
  it("tags two concurrent delegations under the same parent with group size 2", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_child_started",
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
      delegationEvent(
        "e2",
        "delegation_child_started",
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
    ]);

    const items = delegationItems(state.entries);

    expect(items).toHaveLength(2);
    const a = items.find((i) => i.delegationId === "del_a")!;
    const b = items.find((i) => i.delegationId === "del_b")!;
    expect(a.concurrencyGroupSize).toBe(2);
    expect(a.concurrencyIndex).toBe(0);
    expect(b.concurrencyGroupSize).toBe(2);
    expect(b.concurrencyIndex).toBe(1);
  });

  it("drops concurreny fields once one of two delegations reaches terminal state", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_child_started",
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
      delegationEvent(
        "e2",
        "delegation_child_started",
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
    ]);

    // 两条均带并发字段（size=2）
    expect(delegationItems(state.entries).every((i) => i.concurrencyGroupSize === 2)).toBe(true);

    // del_a 进入终态，从 running 集合移除，仅剩 del_b（size<2）
    state = projectTimelineIncrementally(state, [
      delegationEvent(
        "e3",
        "delegation_finished",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "completed",
          summary: "ok",
        },
        3,
      ),
    ]);

    const items = delegationItems(state.entries);
    const a = items.find((i) => i.delegationId === "del_a")!;
    const b = items.find((i) => i.delegationId === "del_b")!;
    expect(a.status).toBe("completed");
    expect(a.concurrencyGroupSize).toBeUndefined();
    expect(a.concurrencyIndex).toBeUndefined();
    // 仅剩 1 个 running，不构成并发组
    expect(b.concurrencyGroupSize).toBeUndefined();
    expect(b.concurrencyIndex).toBeUndefined();
  });

  it("keeps group size 2 after one of three concurrent delegations finishes", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_child_started",
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
      delegationEvent(
        "e2",
        "delegation_child_started",
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
      delegationEvent(
        "e3",
        "delegation_child_started",
        {
          delegation_id: "del_c",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_c",
          child_agent_id: "delegate_coder",
          delegation_type: "coding",
          status: "running",
        },
        3,
      ),
    ]);

    expect(
      delegationItems(state.entries).every((i) => i.concurrencyGroupSize === 3),
    ).toBe(true);

    // 完成 del_b（中间项），剩余 2 个仍构成并发组
    state = projectTimelineIncrementally(state, [
      delegationEvent(
        "e4",
        "delegation_finished",
        {
          delegation_id: "del_b",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_b",
          child_agent_id: "delegate_analyst",
          delegation_type: "analysis",
          status: "completed",
          summary: "ok",
        },
        4,
      ),
    ]);

    const items = delegationItems(state.entries);
    const a = items.find((i) => i.delegationId === "del_a")!;
    const c = items.find((i) => i.delegationId === "del_c")!;
    const b = items.find((i) => i.delegationId === "del_b")!;
    expect(a.concurrencyGroupSize).toBe(2);
    expect(a.concurrencyIndex).toBe(0);
    expect(c.concurrencyGroupSize).toBe(2);
    expect(c.concurrencyIndex).toBe(1);
    expect(b.concurrencyGroupSize).toBeUndefined();
  });

  it("does not double count when the same event is replayed", () => {
    const events = [
      delegationEvent(
        "e1",
        "delegation_child_started",
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
      delegationEvent(
        "e2",
        "delegation_child_started",
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
    let state = projectTimelineIncrementally(createTimelineProjectorState(), events);
    // 同批事件重放（event_id 已在 processedEventIds）→ 幂等，集合不漂移
    state = projectTimelineIncrementally(state, events);

    const set = state.concurrencyByParentTurn.get("turn_parent");
    expect(set).toBeDefined();
    expect(set!.size).toBe(2);
    expect([...set!]).toEqual(["del_a", "del_b"]);
    expect(
      delegationItems(state.entries).every((i) => i.concurrencyGroupSize === 2),
    ).toBe(true);
  });

  it("keeps delegations from different parents in separate groups", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent_1",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          delegation_type: "review",
          status: "running",
        },
        1,
        "turn_parent_1",
      ),
      delegationEvent(
        "e2",
        "delegation_child_started",
        {
          delegation_id: "del_b",
          parent_turn_id: "turn_parent_2",
          child_turn_id: "turn_child_b",
          child_agent_id: "delegate_analyst",
          delegation_type: "analysis",
          status: "running",
        },
        2,
        "turn_parent_2",
      ),
    ]);

    const items = delegationItems(state.entries);
    // 各自 parent 下仅 1 个 running，均不构成并发组
    expect(items.every((i) => i.concurrencyGroupSize === undefined)).toBe(true);
    expect(state.concurrencyByParentTurn.get("turn_parent_1")!.size).toBe(1);
    expect(state.concurrencyByParentTurn.get("turn_parent_2")!.size).toBe(1);
  });
});
