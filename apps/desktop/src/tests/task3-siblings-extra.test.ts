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

describe("deriveSiblingDelegations 边界补充", () => {
  it("3 个 sibling（同 parent）返回长度 3，且各自 childTurnId/childAgentId/status 正确", () => {
    // 目的：覆盖 >2 sibling 的派生正确性，验证每个 sibling 的 childTurnId/childAgentId 均被保留。
    // 可能暴露的缺陷：分组按 index 而非 delegation_id、childAgentId 被后续事件覆盖、长度截断。
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
        "delegation_child_started",
        {
          delegation_id: "del_c",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_c",
          child_agent_id: "delegate_planner",
          status: "running",
        },
        3,
      ),
    ];

    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(3);
    const ids = siblings.map((s) => s.childTurnId).sort();
    expect(ids).toEqual(["turn_child_a", "turn_child_b", "turn_child_c"]);
    expect(siblings.find((s) => s.childTurnId === "turn_child_b")?.childAgentId).toBe(
      "delegate_analyst",
    );
    expect(siblings.find((s) => s.childTurnId === "turn_child_c")?.childAgentId).toBe(
      "delegate_planner",
    );
  });

  it("3 个 sibling 中 1 个终态（cancelled）状态归一正确", () => {
    // 目的：覆盖终态归一在多发 sibling 场景下只影响单个 sibling，且不污染其它 sibling 状态。
    // 可能暴露的缺陷：终态事件误用 delegationStatusFromEvent 错误归一、或污染同组其它 child。
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
        "delegation_child_started",
        {
          delegation_id: "del_c",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_c",
          child_agent_id: "delegate_planner",
          status: "running",
        },
        3,
      ),
      delegationEvent(
        "e4",
        "delegation_cancelled",
        {
          delegation_id: "del_c",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_c",
          child_agent_id: "delegate_planner",
          status: "cancelled",
          error: "user abort",
        },
        4,
      ),
    ];

    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(3);
    const c = siblings.find((s) => s.childTurnId === "turn_child_c")!;
    const a = siblings.find((s) => s.childTurnId === "turn_child_a")!;
    const b = siblings.find((s) => s.childTurnId === "turn_child_b")!;
    expect(c.status).toBe("cancelled");
    expect(a.status).toBe("running");
    expect(b.status).toBe("running");
  });

  it("不同 parent 的 delegation 不混入 sibling 列表（选中 parent_1 的 child）", () => {
    // 目的：独立覆盖 parent 隔离，验证仅与选中 child 同 parent 的 delegation 进入列表。
    // 可能暴露的缺陷：parent_turn_id 比较用错字段、误用 event.turn_id 而非 payload.parent_turn_id。
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

    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(1);
    expect(siblings[0].childTurnId).toBe("turn_child_a");
  });

  it("selectedChildTurnId 无对应 delegation_child_started 时返回 []", () => {
    // 目的：覆盖反查失败（无 delegation_child_started 匹配选中 child）的空数组契约。
    // 可能暴露的缺陷：仅按 event 遍历时误把 delegation_started（无 child_turn_id）当匹配、空指针。
    const events = [
      delegationEvent(
        "e1",
        "delegation_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_agent_id: "delegate_reviewer",
          status: "running",
        },
        1,
      ),
    ];

    expect(deriveSiblingDelegations(events, "turn_child_unknown")).toEqual([]);
  });

  it("选中 child 属于某 parent，但该 parent 下另有不同 child_turn_id 但同 delegation_id 的更新事件时不串号", () => {
    // 目的：验证 child_turn_id 以最新事件为准（来自 byDelegation 的 childTurnId 取最新），
    // 防止 child_turn_id 被早期事件错误定格导致 sibling 指向错误 child。
    // 可能暴露的缺陷：childTurnId 取首次事件而非最新、导致 tab 点击切到陈旧 childTurnId。
    const events = [
      delegationEvent(
        "e1",
        "delegation_child_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_a_old",
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
        "delegation_child_started",
        {
          delegation_id: "del_b",
          parent_turn_id: "turn_parent",
          child_turn_id: "turn_child_b",
          child_agent_id: "delegate_analyst",
          status: "running",
        },
        3,
      ),
    ];

    const siblings = deriveSiblingDelegations(events, "turn_child_a");
    expect(siblings).toHaveLength(2);
    const a = siblings.find((s) => s.childAgentId === "delegate_reviewer")!;
    expect(a.childTurnId).toBe("turn_child_a");
  });
});
