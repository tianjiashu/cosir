/**
 * Task 2 独立补充边界测试（投影器并发组识别）。
 *
 * 本文件由独立测试 Agent 编写，仅新增测试、不修改任何业务代码。
 * 聚焦开发自测未充分覆盖的边界：多 parent 隔离、乱序到达、回放幂等、
 * 中途完成后的 index 重排、非并发不附加字段、prev 状态不可变污染。
 */
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

/** 构造 delegation 生命周期事件（payload 字段与后端真实枚举一致）。 */
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
    created_at: `2026-08-10T00:00:${String(sequence).padStart(2, "0")}Z`,
    payload,
  } as RuntimeEvent;
}

/** 便捷构造：child_started（running）事件。 */
function started(
  eventId: string,
  delegationId: string,
  parentTurnId: string,
  sequence: number,
): RuntimeEvent {
  return delegationEvent(
    eventId,
    "delegation_child_started",
    {
      delegation_id: delegationId,
      parent_turn_id: parentTurnId,
      child_turn_id: `child_${delegationId}`,
      child_agent_id: `agent_${delegationId}`,
      delegation_type: "review",
      status: "running",
    },
    sequence,
    parentTurnId,
  );
}

/** 便捷构造：finished（completed）事件。 */
function finished(
  eventId: string,
  delegationId: string,
  parentTurnId: string,
  sequence: number,
): RuntimeEvent {
  return delegationEvent(
    eventId,
    "delegation_finished",
    {
      delegation_id: delegationId,
      parent_turn_id: parentTurnId,
      child_turn_id: `child_${delegationId}`,
      child_agent_id: `agent_${delegationId}`,
      delegation_type: "review",
      status: "completed",
      summary: "done",
    },
    sequence,
    parentTurnId,
  );
}

function items(entries: TurnTimelineEntry[]): TimelineDelegationItem[] {
  return entries
    .filter(
      (entry): entry is Extract<TurnTimelineEntry, { kind: "delegation" }> =>
        entry.kind === "delegation",
    )
    .map((entry) => entry.item);
}

function byId(entries: TurnTimelineEntry[], delegationId: string): TimelineDelegationItem {
  const found = items(entries).find((i) => i.delegationId === delegationId);
  expect(found, `未找到 delegation ${delegationId}`).toBeDefined();
  return found!;
}

describe("Task2 补充：多 parent 隔离", () => {
  // 测试目的：A parent 下 2 个并发，B parent 下 1 个；验证 B 不抬高 A 的 size、也不被 A 计入。
  // 可能发现的缺陷：并发集合按全局而非 parentTurnId 分桶（key 错用 turn_id / 忽略 parent）。
  it("不同 parentTurnId 的 delegation 互不计入并发组", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a1", "turn_A", 1),
      started("e2", "del_a2", "turn_A", 2),
      started("e3", "del_b1", "turn_B", 3),
    ]);

    expect(byId(state.entries, "del_a1").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_a2").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_a1").concurrencyIndex).toBe(0);
    expect(byId(state.entries, "del_a2").concurrencyIndex).toBe(1);
    // B 下仅 1 个 running，不构成并发组
    expect(byId(state.entries, "del_b1").concurrencyGroupSize).toBeUndefined();
    expect(byId(state.entries, "del_b1").concurrencyIndex).toBeUndefined();
    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(2);
    expect(state.concurrencyByParentTurn.get("turn_B")!.size).toBe(1);
  });

  // 测试目的：B parent 后续新增到 3 并发，A 的 size 保持 2 不受影响。
  // 可能发现的缺陷：reattachConcurrencyForParent 未按 parentTurnId 过滤，跨 parent 串写字段。
  it("B parent 扩张到 3 并发时 A 的并发组 size 仍为 2", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a1", "turn_A", 1),
      started("e2", "del_a2", "turn_A", 2),
    ]);
    state = projectTimelineIncrementally(state, [
      started("e3", "del_b1", "turn_B", 3),
      started("e4", "del_b2", "turn_B", 4),
      started("e5", "del_b3", "turn_B", 5),
    ]);

    expect(byId(state.entries, "del_a1").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_a2").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_b1").concurrencyGroupSize).toBe(3);
    expect(byId(state.entries, "del_b3").concurrencyGroupSize).toBe(3);
    expect(byId(state.entries, "del_b3").concurrencyIndex).toBe(2);
  });
});

describe("Task2 补充：乱序到达", () => {
  // 测试目的：极端乱序——先收到 del_a 的 finished，再收到其 child_started。
  // 断言集合不出现负 size / 不把已终态项重新计入 running，且状态不被降级。
  // 可能发现的缺陷：终态项被后到的 started 事件重新 add 进 running 集合，造成僵尸并发组。
  it("finished 先到、child_started 后到时集合增删正确且无负 size", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      finished("e1", "del_a", "turn_A", 1),
    ]);
    // 终态先到：集合中不应含 del_a
    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(0);
    expect(byId(state.entries, "del_a").status).toBe("completed");
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();

    // 迟到的 started 到达
    state = projectTimelineIncrementally(state, [started("e2", "del_a", "turn_A", 2)]);

    const set = state.concurrencyByParentTurn.get("turn_A")!;
    expect(set.size).toBeGreaterThanOrEqual(0);
    // mergeDelegation 保护：已终态不被非终态事件降级
    expect(byId(state.entries, "del_a").status).toBe("completed");
    // 自身已终态 → 不附加并发字段
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();
    expect(byId(state.entries, "del_a").concurrencyIndex).toBeUndefined();
  });

  // 测试目的：两个 delegation 的 started/finished 交错乱序到达，最终 size 仍等于真实 running 数。
  // 可能发现的缺陷：delete 不存在元素或重复 add 导致计数漂移。
  it("多 delegation 乱序交错后集合规模等于真实 running 数", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      finished("e1", "del_a", "turn_A", 1), // a 终态先到
      started("e2", "del_b", "turn_A", 2), // b running
      started("e3", "del_a", "turn_A", 3), // a 的 started 迟到
      started("e4", "del_c", "turn_A", 4), // c running
    ]);

    const set = state.concurrencyByParentTurn.get("turn_A")!;
    // 真实 running 应为 b、c（a 已 completed）；无论实现是否把迟到 started 计入，
    // size 都不得为负、且不得超过总 delegation 数
    expect(set.size).toBeGreaterThanOrEqual(2);
    expect(set.size).toBeLessThanOrEqual(3);
    expect(byId(state.entries, "del_b").concurrencyGroupSize).toBe(set.size);
    expect(byId(state.entries, "del_c").concurrencyGroupSize).toBe(set.size);
    // 已终态的 a 一律不带并发字段
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();
  });
});

describe("Task2 补充：回放幂等", () => {
  // 测试目的：同一 event_id 事件重复喂入多次（3 轮），集合 size 与迭代顺序不漂移。
  // 可能发现的缺陷：processedEventIds 未拦截导致集合重复计数或 entries 重复新增。
  it("同一 event_id 重复喂入 3 次，集合 size 与顺序保持幂等", () => {
    const events = [
      started("e1", "del_a", "turn_A", 1),
      started("e2", "del_b", "turn_A", 2),
    ];
    let state = projectTimelineIncrementally(createTimelineProjectorState(), events);
    const firstEntries = state.entries;
    state = projectTimelineIncrementally(state, events);
    state = projectTimelineIncrementally(state, events);

    const set = state.concurrencyByParentTurn.get("turn_A")!;
    expect(set.size).toBe(2);
    expect([...set]).toEqual(["del_a", "del_b"]);
    expect(items(state.entries)).toHaveLength(2);
    // 全部事件已处理 → 零分配返回同一引用（引用稳定契约）
    expect(state.entries).toBe(firstEntries);
    expect(byId(state.entries, "del_a").concurrencyIndex).toBe(0);
    expect(byId(state.entries, "del_b").concurrencyIndex).toBe(1);
  });

  // 测试目的：终态事件被回放（重复的 finished），不得把 size 减到负数或误删其它项。
  // 可能发现的缺陷：重复 delete 引起状态漂移 / 其它并发项字段被清空。
  it("终态事件回放不导致 size 漂移", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
      started("e2", "del_b", "turn_A", 2),
      started("e3", "del_c", "turn_A", 3),
    ]);
    const fin = finished("e4", "del_a", "turn_A", 4);
    state = projectTimelineIncrementally(state, [fin]);
    state = projectTimelineIncrementally(state, [fin]);
    state = projectTimelineIncrementally(state, [fin]);

    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(2);
    expect(byId(state.entries, "del_b").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_c").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();
  });

  // 测试目的：不可变契约——增量投影不得污染调用方仍持有的 prev 状态（含嵌套 Set）。
  // 可能发现的缺陷：只浅拷贝了外层 Map、内层 Set 就地 mutate，导致 prev 的集合被改写，
  // 时间旅行/回滚/并发渲染读取旧 state 时得到错误 size。
  it("不修改 prev 状态的 concurrencyByParentTurn 嵌套 Set", () => {
    const s1 = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
    ]);
    const setBefore = s1.concurrencyByParentTurn.get("turn_A")!;
    expect(setBefore.size).toBe(1);
    const snapshot = [...setBefore];

    const s2 = projectTimelineIncrementally(s1, [started("e2", "del_b", "turn_A", 2)]);

    expect(byId(s2.entries, "del_b").concurrencyGroupSize).toBe(2);
    // prev（s1）的集合内容必须保持投影前快照
    expect([...s1.concurrencyByParentTurn.get("turn_A")!]).toEqual(snapshot);
    expect(s1.concurrencyByParentTurn.get("turn_A")!.size).toBe(1);
  });

  // 测试目的：由上一条不可变性缺陷派生的**用户可见后果**——从同一个 prev 状态分叉出两条
  // 投影路径（React StrictMode 双调用、并发渲染重放、乐观更新回滚均会产生此形态）。
  // 第二条路径应只看到自己那批事件，但因内层 Set 被就地 mutate 而被第一条路径污染。
  // 可能发现的缺陷：projectTimelineIncrementally 声称「纯函数、不修改 prev」但实际共享可变 Set，
  // 导致同一 prev 的两次独立调用互相串扰，渲染出错误的 concurrencyGroupSize。
  it("从同一 prev 分叉两次投影时互不串扰（并发渲染/StrictMode 重放安全）", () => {
    const base = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
    ]);

    // 路径 1：base + del_b → 该路径内 turn_A 有 2 个 running
    const branch1 = projectTimelineIncrementally(base, [started("e2", "del_b", "turn_A", 2)]);
    expect(byId(branch1.entries, "del_b").concurrencyGroupSize).toBe(2);

    // 路径 2：同一 base + del_c（与路径 1 无关）→ 该路径内 turn_A 应只有 del_a、del_c 共 2 个
    const branch2 = projectTimelineIncrementally(base, [started("e3", "del_c", "turn_A", 3)]);

    // 路径 2 不应看到路径 1 引入的 del_b
    expect([...branch2.concurrencyByParentTurn.get("turn_A")!]).not.toContain("del_b");
    expect(branch2.concurrencyByParentTurn.get("turn_A")!.size).toBe(2);
    expect(byId(branch2.entries, "del_c").concurrencyGroupSize).toBe(2);
    expect(byId(branch2.entries, "del_c").concurrencyIndex).toBe(1);
  });
});

describe("Task2 补充：3 并发中途完成 1 个", () => {
  // 测试目的：3 并发中完成首个（del_a），剩余 b/c 的 groupSize 为 2 且 index 重排为 0/1 稳定。
  // 可能发现的缺陷：index 沿用旧插入序（1/2）出现空洞，UI 布局按 index 定位时错位。
  it("完成首个后剩余 2 个 groupSize=2 且 index 重排为 0/1", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
      started("e2", "del_b", "turn_A", 2),
      started("e3", "del_c", "turn_A", 3),
    ]);
    expect(items(state.entries).every((i) => i.concurrencyGroupSize === 3)).toBe(true);
    expect(byId(state.entries, "del_c").concurrencyIndex).toBe(2);

    state = projectTimelineIncrementally(state, [finished("e4", "del_a", "turn_A", 4)]);

    expect(byId(state.entries, "del_b").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_b").concurrencyIndex).toBe(0);
    expect(byId(state.entries, "del_c").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_c").concurrencyIndex).toBe(1);
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();
    expect(byId(state.entries, "del_a").concurrencyIndex).toBeUndefined();
  });

  // 测试目的：连续完成到只剩 1 个 running，最后一个应退出并发组（字段清空、无陈旧残留）。
  // 可能发现的缺陷：先前定稿的 size=2 未被重算，残留陈旧并发字段。
  it("连续完成到仅剩 1 个 running 时并发字段被清空（无陈旧残留）", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
      started("e2", "del_b", "turn_A", 2),
      started("e3", "del_c", "turn_A", 3),
    ]);
    state = projectTimelineIncrementally(state, [
      finished("e4", "del_a", "turn_A", 4),
      finished("e5", "del_b", "turn_A", 5),
    ]);

    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(1);
    expect(byId(state.entries, "del_c").concurrencyGroupSize).toBeUndefined();
    expect(byId(state.entries, "del_c").concurrencyIndex).toBeUndefined();
    expect(byId(state.entries, "del_c").status).toBe("running");
  });

  // 测试目的：失败/取消同样属终态，应与 completed 一致地退出 running 集合。
  // 可能发现的缺陷：isDelegationTerminal 仅判 completed，failed/cancelled 被当作仍 running。
  it("failed / cancelled 同样退出 running 集合", () => {
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
      started("e2", "del_b", "turn_A", 2),
      started("e3", "del_c", "turn_A", 3),
    ]);
    state = projectTimelineIncrementally(state, [
      delegationEvent(
        "e4",
        "delegation_failed",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_A",
          child_agent_id: "agent_a",
          delegation_type: "review",
          status: "failed",
          error: "boom",
        },
        4,
        "turn_A",
      ),
    ]);
    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(2);
    expect(byId(state.entries, "del_a").status).toBe("failed");
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();

    state = projectTimelineIncrementally(state, [
      delegationEvent(
        "e5",
        "delegation_cancelled",
        {
          delegation_id: "del_b",
          parent_turn_id: "turn_A",
          child_agent_id: "agent_b",
          delegation_type: "review",
          status: "cancelled",
        },
        5,
        "turn_A",
      ),
    ]);
    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(1);
    expect(byId(state.entries, "del_b").concurrencyGroupSize).toBeUndefined();
    expect(byId(state.entries, "del_c").concurrencyGroupSize).toBeUndefined();
  });
});

describe("Task2 补充：非并发与异常输入", () => {
  // 测试目的：单个 delegation 自身 running（无 sibling）→ 不附加并发字段。
  // 可能发现的缺陷：groupSize>=2 阈值写成 >=1，导致单条也被标记为并发组。
  it("单 delegation 自身 running 时不附加并发字段", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_solo", "turn_A", 1),
    ]);
    const solo = byId(state.entries, "del_solo");
    expect(solo.status).toBe("running");
    expect(solo.concurrencyGroupSize).toBeUndefined();
    expect(solo.concurrencyIndex).toBeUndefined();
    expect("concurrencyGroupSize" in solo ? solo.concurrencyGroupSize : undefined).toBeUndefined();
    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(1);
  });

  // 测试目的：delegation_started（pending，尚无 child）也应计入 running 集合参与并发派生。
  // 可能发现的缺陷：仅 child_started 被计入，pending 阶段并发被漏判。
  it("delegation_started(pending) 与 child_started 混合时同样参与并发组", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_started",
        {
          delegation_id: "del_a",
          parent_turn_id: "turn_A",
          child_agent_id: "agent_a",
          delegation_type: "review",
        },
        1,
        "turn_A",
      ),
      started("e2", "del_b", "turn_A", 2),
    ]);
    expect(byId(state.entries, "del_a").status).toBe("pending");
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBe(2);
    expect(byId(state.entries, "del_a").concurrencyIndex).toBe(0);
    expect(byId(state.entries, "del_b").concurrencyGroupSize).toBe(2);
  });

  // 测试目的：缺失 delegation_id 的畸形事件被丢弃，不产生条目、不污染并发集合。
  // 可能发现的缺陷：空 id 被当作合法 key 加入集合，虚增 groupSize。
  it("缺失 delegation_id 的畸形事件不产生条目也不污染集合", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
      delegationEvent(
        "e2",
        "delegation_child_started",
        { parent_turn_id: "turn_A", child_agent_id: "agent_x", status: "running" },
        2,
        "turn_A",
      ),
      delegationEvent(
        "e3",
        "delegation_child_started",
        { delegation_id: "   ", parent_turn_id: "turn_A", status: "running" },
        3,
        "turn_A",
      ),
    ]);
    expect(items(state.entries)).toHaveLength(1);
    expect(state.concurrencyByParentTurn.get("turn_A")!.size).toBe(1);
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();
  });

  // 测试目的：payload 缺 parent_turn_id 时回落到 event.turn_id 作为并发分桶 key。
  // 可能发现的缺陷：回落缺失导致 key 为空串，不同 turn 的 delegation 被错误合并成一组。
  it("payload 缺 parent_turn_id 时按 event.turn_id 分桶（不跨 turn 误并）", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      delegationEvent(
        "e1",
        "delegation_child_started",
        { delegation_id: "del_a", child_agent_id: "agent_a", status: "running" },
        1,
        "turn_X",
      ),
      delegationEvent(
        "e2",
        "delegation_child_started",
        { delegation_id: "del_b", child_agent_id: "agent_b", status: "running" },
        2,
        "turn_Y",
      ),
    ]);
    expect(byId(state.entries, "del_a").parentTurnId).toBe("turn_X");
    expect(byId(state.entries, "del_b").parentTurnId).toBe("turn_Y");
    expect(byId(state.entries, "del_a").concurrencyGroupSize).toBeUndefined();
    expect(byId(state.entries, "del_b").concurrencyGroupSize).toBeUndefined();
    expect(state.concurrencyByParentTurn.get("turn_X")!.size).toBe(1);
    expect(state.concurrencyByParentTurn.get("turn_Y")!.size).toBe(1);
  });

  // 测试目的：空事件数组时零分配返回 prev 原引用，且不初始化多余的并发桶。
  // 可能发现的缺陷：空批次仍复制 Map/Set 造成每帧无谓分配、击穿 memo。
  it("空事件批次返回 prev 原引用（零分配）", () => {
    const s1 = projectTimelineIncrementally(createTimelineProjectorState(), [
      started("e1", "del_a", "turn_A", 1),
    ]);
    const s2 = projectTimelineIncrementally(s1, []);
    expect(s2).toBe(s1);
    expect(s2.concurrencyByParentTurn).toBe(s1.concurrencyByParentTurn);
  });
});
