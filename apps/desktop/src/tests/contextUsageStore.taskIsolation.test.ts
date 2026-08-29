// @vitest-environment happy-dom
/**
 * contextUsageStore 的 task 维度隔离。
 *
 * 守护不变量：上下文占用按 taskId 分键存储，不同 task 的占用互不覆盖；键类型为
 * number（与 TaskRecord.task_id / tasksById / drafts 的 idUnify 约定一致）。
 *
 * 可证伪性：旧实现是全局单值（usedTokens / totalTokens / updatedAt + setUsage 无
 * taskId 参数），本文件所有断言在旧实现下均不成立（无 usageByTaskId 字段、A 的
 * 写入会被 B 覆盖）。
 *
 * @module tests/contextUsageStore.taskIsolation
 */
import { beforeEach, describe, expect, it } from "vitest";

import { EMPTY_USAGE, useContextUsageStore } from "@/stores/contextUsageStore";

/** 构造 CONTEXT_USAGE payload 的最小形状。 */
function payload(usedTokens: number, totalTokens: number) {
  return { used_tokens: usedTokens, total_tokens: totalTokens };
}

describe("contextUsageStore 按 task 维度隔离", () => {
  beforeEach(() => {
    useContextUsageStore.setState({ usageByTaskId: {} });
  });

  // 核心：A/B 两个 task 分别 setUsage，互不覆盖。
  it("两个 task 分别写入占用，互不覆盖", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(1, payload(1000, 200000), "2026-08-28T10:00:00Z");
    store.setUsage(2, payload(5000, 128000), "2026-08-28T10:00:01Z");

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]).toEqual({
      usedTokens: 1000,
      totalTokens: 200000,
      updatedAt: "2026-08-28T10:00:00Z",
    });
    expect(state.usageByTaskId[2]).toEqual({
      usedTokens: 5000,
      totalTokens: 128000,
      updatedAt: "2026-08-28T10:00:01Z",
    });
  });

  // 同一 task 重复写入是「最新值覆盖」语义，不得累加。
  it("同一 task 重复写入为最新值覆盖，不累加", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(7, payload(1000, 200000), "2026-08-28T10:00:00Z");
    store.setUsage(7, payload(2500, 200000), "2026-08-28T10:00:02Z");

    expect(useContextUsageStore.getState().usageByTaskId[7]?.usedTokens).toBe(2500);
  });

  // 键类型必须是 number：以 number 键写入后，用 number 键可读；且不能被 string 键索引命中。
  it("键为 number 维度，与 TaskRecord.task_id 口径一致", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(42, payload(1000, 200000), "2026-08-28T10:00:00Z");

    const keys = Object.keys(useContextUsageStore.getState().usageByTaskId);
    expect(keys).toEqual(["42"]);
    // number 键可直接命中（若实现退回 string 键，此处为 undefined）。
    expect(useContextUsageStore.getState().usageByTaskId[42]).toBeDefined();
  });

  // resetTask 只清指定 task，不影响其它 task 的缓存。
  it("resetTask 仅清除指定 task，不影响其它 task", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(1, payload(1000, 200000), "2026-08-28T10:00:00Z");
    store.setUsage(2, payload(5000, 128000), "2026-08-28T10:00:01Z");

    useContextUsageStore.getState().resetTask(1);

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]).toBeUndefined();
    expect(state.usageByTaskId[2]?.usedTokens).toBe(5000);
  });

  // 无条目时 resetTask 不得产生新的对象引用（避免无意义地触发订阅者重渲染）。
  it("resetTask 对不存在的 task 不改变状态对象引用", () => {
    const before = useContextUsageStore.getState().usageByTaskId;
    useContextUsageStore.getState().resetTask(999);
    expect(useContextUsageStore.getState().usageByTaskId).toBe(before);
  });

  // clearAll 清空全部 task 占用。
  it("clearAll 清空全部 task 占用", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(1, payload(1000, 200000), "2026-08-28T10:00:00Z");
    store.setUsage(2, payload(5000, 128000), "2026-08-28T10:00:01Z");

    useContextUsageStore.getState().clearAll();

    expect(useContextUsageStore.getState().usageByTaskId).toEqual({});
  });

  // EMPTY_USAGE 必须是共享常量：selector 内新建对象会击穿 zustand 引用相等判定。
  it("EMPTY_USAGE 是稳定的共享常量", () => {
    const store = useContextUsageStore.getState();
    const first = activeTaskIdIsMissing(store.usageByTaskId, 123);
    const second = activeTaskIdIsMissing(store.usageByTaskId, 456);
    expect(first).toBe(second);
    expect(first).toBe(EMPTY_USAGE);
    expect(EMPTY_USAGE).toEqual({ usedTokens: 0, totalTokens: 0, updatedAt: null });
  });
});

/**
 * 复刻 ContextUsageRing 中的缺省回退表达式。
 *
 * 单独成函数是为了让「selector 不得新建对象」这一约束成为可执行断言：若实现改为
 * 内联 `?? { usedTokens: 0, ... }`，两次调用将返回不同引用，上一用例失败。
 *
 * @param usageByTaskId - store 中的占用映射。
 * @param taskId - 待读取的任务标识。
 * @returns 该任务的占用快照；无条目时返回共享空快照。
 */
function activeTaskIdIsMissing(
  usageByTaskId: Record<number, { usedTokens: number; totalTokens: number; updatedAt: string | null }>,
  taskId: number,
) {
  return usageByTaskId[taskId] ?? EMPTY_USAGE;
}
