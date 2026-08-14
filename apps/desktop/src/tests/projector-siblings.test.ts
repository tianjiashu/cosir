import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { deriveSiblingDelegations } from "@/services/timeline/projector";

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

describe("deriveSiblingDelegations", () => {
  it("单 child（无并发 sibling）返回仅含选中 child 自身的列表（长度 1，UI 按 >=2 不显示 tab）", () => {
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
    ];
    // 选中项存在，同 parent 下只有它一个 child → 列表仅含自身（长度 1）；
    // 是否渲染并发 tab 由 SubagentPanel 的「siblings.length >= 2」判定（非 UI 显示即隐藏）。
    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(1);
    expect(siblings[0].childTurnId).toBe("turn_child_a");
    expect(siblings[0].childAgentId).toBe("delegate_reviewer");
  });

  it("选中态为 null 时返回空数组", () => {
    const events = [
      delegationEvent(
        "e1",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          status: "running",
        },
        1,
      ),
    ];
    expect(deriveSiblingDelegations(events, "")).toEqual([]);
  });

  it("同 parent 下 2 个 child 返回长度 2，含各自 childTurnId/status", () => {
    const events = [
      delegationEvent(
        "e1",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
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
          status: "running",
        },
        2,
      ),
    ];
    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(2);
    const a = siblings.find((s) => s.childTurnId === "turn_child_a")!;
    const b = siblings.find((s) => s.childTurnId === "turn_child_b")!;
    expect(a.childAgentId).toBe("delegate_reviewer");
    expect(b.childAgentId).toBe("delegate_analyst");
    expect(a.status).toBe("running");
    expect(b.status).toBe("running");
  });

  it("不同 parent 的 delegation 不混入 sibling 列表", () => {
    const events = [
      delegationEvent(
        "e1",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent_1",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
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
          status: "running",
        },
        2,
        "turn_parent_2",
      ),
    ];
    // 选中 turn_parent_1 下的 child_a，turn_parent_2 的 child_b 不应混入。
    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(1);
    expect(siblings[0].childTurnId).toBe("turn_child_a");
  });

  it("终态 child 状态正确归一（delegation_finished → completed）", () => {
    const events = [
      delegationEvent(
        "e1",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
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
          status: "running",
        },
        2,
      ),
      delegationEvent(
        "e3",
        "delegation_finished",
        {
          delegation_id: "del_b",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_b",
          child_agent_id: "delegate_analyst",
          status: "completed",
          summary: "done",
        },
        3,
      ),
    ];
    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(2);
    const a = siblings.find((s) => s.childTurnId === "turn_child_a")!;
    const b = siblings.find((s) => s.childTurnId === "turn_child_b")!;
    expect(a.status).toBe("running");
    expect(b.status).toBe("completed");
  });

  it("按 delegation_id 取最新状态，不取更早的同组事件", () => {
    const events = [
      delegationEvent(
        "e1",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          status: "running",
        },
        1,
      ),
      delegationEvent(
        "e2",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          status: "running",
        },
        2,
      ),
      delegationEvent(
        "e3",
        "delegation_failed",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a",
          child_agent_id: "delegate_reviewer",
          status: "failed",
          error: "boom",
        },
        3,
      ),
    ];
    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(1);
    expect(siblings[0].status).toBe("failed");
  });
});
