// @vitest-environment happy-dom
/**
 * H1 回归护栏：`streamingTurnIds` 的按 task 维度隔离语义。
 *
 * 原缺陷：turnStore 用单一 cross-task 字段 `streamingTurnId: string | null` 记录
 * 「当前正在接收 SSE 的轮次」。桌面端支持多 task 并发流式，故：
 * - task B 开始流式会**覆盖** task A 的标记 → A 的 UI 失去流式态（停止指示器、
 *   输入框误解锁）；
 * - task B 到达终态调用 `setStreamingTurn(null)` 会**误清** task A 仍在进行的标记
 *   → A 明明还在输出，UI 却显示已结束。
 *
 * 修复形态：改为 `streamingTurnIds: Record<taskId, turnId>`，
 * `setStreamingTurn(taskId, turnId | null)` 只操作该 taskId 对应的键，
 * 传 null 时 `delete` 该键而不触碰其它 task。
 *
 * 本文件直接对 store action 做断言（纯状态逻辑，无需渲染组件）。
 *
 * @module tests/turnStore.streamingTurnIds
 */

import { beforeEach, describe, expect, it } from "vitest";
import type { TurnRecord } from "@shared/turn";

import { useTurnStore } from "@/stores/turnStore";

/** 构造一个最小可用的 TurnRecord。 */
function makeTurn(taskId: string, turnId: string, status = "running"): TurnRecord {
  return {
    turn_id: turnId,
    task_id: taskId,
    input_text: "hi",
    status,
    end_reason: null,
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TurnRecord;
}

/** 读取当前 streamingTurnIds 映射。 */
function streaming(): Record<string, string> {
  return useTurnStore.getState().streamingTurnIds;
}

beforeEach(() => {
  // 每个用例从干净状态开始，避免 zustand 单例跨用例污染。
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
});

describe("turnStore - H1：streamingTurnIds 形态与初始值", () => {
  it("初始状态：streamingTurnIds 为空对象（而非 null/undefined 的旧单值字段）", () => {
    // 测试目的：锁死数据结构已从 `streamingTurnId: string|null` 迁移为 Record；
    // 可能发现缺陷：修复未落地或部分回退，导致下游 `streamingTurnIds[taskId]` 读到
    //   undefined 上的属性访问异常。
    const state = useTurnStore.getState();
    expect(state.streamingTurnIds).toEqual({});
    expect(typeof state.streamingTurnIds).toBe("object");
    expect(state.streamingTurnIds).not.toBeNull();
    // 旧的单值字段必须已彻底移除，否则存在两套真源（读旧字段的组件将永久失效）。
    expect("streamingTurnId" in (state as unknown as Record<string, unknown>)).toBe(false);
  });

  it("setStreamingTurn 签名为 (taskId, turnId)：接受两个参数", () => {
    // 测试目的：签名契约——调用方（useTask/useSSE）必须能按 task 维度写入；
    // 可能发现缺陷：签名仍是 (turnId) 单参，则第二参被忽略、按 task 隔离完全失效。
    expect(useTurnStore.getState().setStreamingTurn).toBeTypeOf("function");
    expect(useTurnStore.getState().setStreamingTurn.length).toBe(2);
  });
});

describe("turnStore - H1：单 task 的写入与清除", () => {
  it("setStreamingTurn('taskA','turnA')：streamingTurnIds['taskA'] === 'turnA'", () => {
    // 测试目的：最基本的写入契约；可能发现缺陷：key 用了 turnId 而非 taskId（映射方向反了）。
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    expect(streaming()["taskA"]).toBe("turnA");
    expect(streaming()).toEqual({ taskA: "turnA" });
  });

  it("同一 task 再次写入不同 turn：覆盖为新 turn（同 task 内 turn 串行）", () => {
    // 测试目的：同一 task 的 turn 是串行的，新 turn 应替换旧 turn 而非并存；
    // 可能发现缺陷：写成数组/累加，导致同 task 出现两个 streaming turn，UI 双重指示器。
    useTurnStore.getState().setStreamingTurn("taskA", "turn-1");
    useTurnStore.getState().setStreamingTurn("taskA", "turn-2");
    expect(streaming()["taskA"]).toBe("turn-2");
    expect(Object.keys(streaming())).toEqual(["taskA"]);
  });

  it("setStreamingTurn('taskA', null)：移除该 key（而非置为 null）", () => {
    // 测试目的：清除语义必须是 delete key，使 `taskId in streamingTurnIds` 可作为
    //   「该 task 是否流式中」的判据；可能发现缺陷：置为 null 后 in 判定仍为 true，
    //   下游误判为仍在流式。
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    useTurnStore.getState().setStreamingTurn("taskA", null);
    expect(streaming()["taskA"]).toBeUndefined();
    expect("taskA" in streaming()).toBe(false);
    expect(streaming()).toEqual({});
  });

  it("清除不存在的 task：no-op 且不改变状态引用（避免无谓重渲染）", () => {
    // 测试目的：幂等清除 + 引用稳定性。zustand 订阅者按引用比较，
    //   无变化时返回同一 state 可避免整棵订阅树重渲染。
    // 可能发现缺陷：无条件展开新对象，导致每次终态回调都触发全量重渲染（性能退化）。
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    const before = streaming();
    useTurnStore.getState().setStreamingTurn("taskZ", null);
    const after = streaming();
    expect(after).toBe(before);
    expect(after).toEqual({ taskA: "turnA" });
  });
});

describe("turnStore - H1：多 task 并发隔离（原缺陷的核心场景）", () => {
  it("写入 taskB 后 taskA 仍保留（不被覆盖）", () => {
    // 测试目的：H1 的核心断言之一——并发写入互不覆盖。
    // 可能发现缺陷：若回退为单值字段，taskA 的标记会被 taskB 抹掉，
    //   taskA 的 UI 立即失去流式态（停止按钮消失、输入框误解锁）。
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    useTurnStore.getState().setStreamingTurn("taskB", "turnB");
    expect(streaming()["taskA"]).toBe("turnA");
    expect(streaming()["taskB"]).toBe("turnB");
    expect(streaming()).toEqual({ taskA: "turnA", taskB: "turnB" });
  });

  it("清除 taskA 后 taskB 仍保留（不被误清）", () => {
    // 测试目的：H1 的核心断言之二——终态清除只影响自己那一个 task。
    // 可能发现缺陷：若 setStreamingTurn(null) 清空整个映射（或回退单值字段置 null），
    //   taskB 明明还在输出，UI 却显示已结束（原 bug 表现）。
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    useTurnStore.getState().setStreamingTurn("taskB", "turnB");
    useTurnStore.getState().setStreamingTurn("taskA", null);
    expect("taskA" in streaming()).toBe(false);
    expect(streaming()["taskB"]).toBe("turnB");
    expect(streaming()).toEqual({ taskB: "turnB" });
  });

  it("三 task 并发：逐个终态清除，每次只减少一个 key", () => {
    // 测试目的：把隔离性放大到 3 个 task，逐步验证「每次清除恰好减 1」的不变式；
    // 可能发现缺陷：清除逻辑用了 filter/重建导致顺序或数量异常（一次清掉多个）。
    const store = useTurnStore.getState();
    store.setStreamingTurn("taskA", "turnA");
    store.setStreamingTurn("taskB", "turnB");
    store.setStreamingTurn("taskC", "turnC");
    expect(Object.keys(streaming()).sort()).toEqual(["taskA", "taskB", "taskC"]);

    store.setStreamingTurn("taskB", null);
    expect(Object.keys(streaming()).sort()).toEqual(["taskA", "taskC"]);

    store.setStreamingTurn("taskC", null);
    expect(Object.keys(streaming()).sort()).toEqual(["taskA"]);
    expect(streaming()["taskA"]).toBe("turnA");

    store.setStreamingTurn("taskA", null);
    expect(streaming()).toEqual({});
  });

  it("交错时序（A 开始 → B 开始 → A 结束 → B 结束）：全程各自独立", () => {
    // 测试目的：还原真实并发时序（用户在 A 提问后切到 B 提问，A 先返回）。
    // 可能发现缺陷：任何一步的写/清越界，都会在此交错序列中暴露为错误快照。
    const store = useTurnStore.getState();
    const snapshots: Array<Record<string, string>> = [];

    store.setStreamingTurn("taskA", "turnA1");
    snapshots.push({ ...streaming() });
    store.setStreamingTurn("taskB", "turnB1");
    snapshots.push({ ...streaming() });
    store.setStreamingTurn("taskA", null);
    snapshots.push({ ...streaming() });
    store.setStreamingTurn("taskB", null);
    snapshots.push({ ...streaming() });

    expect(snapshots).toEqual([
      { taskA: "turnA1" },
      { taskA: "turnA1", taskB: "turnB1" },
      { taskB: "turnB1" },
      {},
    ]);
  });

  it("同一 turnId 被两个不同 task 使用（防御性）：按 taskId 独立存储", () => {
    // 测试目的：即使出现 turnId 重复（异常/测试数据），也应按 taskId 分别存储，
    //   证明 key 确实是 taskId 而非 turnId。
    // 可能发现缺陷：映射方向写反（Record<turnId, taskId>），两 task 互相踩踏。
    useTurnStore.getState().setStreamingTurn("taskA", "same-turn");
    useTurnStore.getState().setStreamingTurn("taskB", "same-turn");
    expect(streaming()).toEqual({ taskA: "same-turn", taskB: "same-turn" });
    useTurnStore.getState().setStreamingTurn("taskA", null);
    expect(streaming()).toEqual({ taskB: "same-turn" });
  });
});

describe("turnStore - H1：与其它 action 的正交性（无副作用串扰）", () => {
  it("setStreamingTurn 不修改 turnsByTaskId", () => {
    // 测试目的：状态副作用边界——流式标记与 turn 列表是两片独立状态。
    // 可能发现缺陷：action 内误重建 turnsByTaskId，导致列表引用变化引发大面积重渲染。
    useTurnStore.getState().setTurnsForTask("taskA", [makeTurn("taskA", "turnA")]);
    const turnsBefore = useTurnStore.getState().turnsByTaskId;
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    expect(useTurnStore.getState().turnsByTaskId).toBe(turnsBefore);
    useTurnStore.getState().setStreamingTurn("taskA", null);
    expect(useTurnStore.getState().turnsByTaskId).toBe(turnsBefore);
  });

  it("updateTurn 不影响 streamingTurnIds", () => {
    // 测试目的：反向正交性——更新 turn 状态（如写 failed）不得顺手清流式标记，
    //   清标记必须由显式的 setStreamingTurn 负责（职责单一）。
    // 可能发现缺陷：updateTurn 内隐式清理，造成多 task 场景下的跨 task 误清。
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    useTurnStore.getState().setStreamingTurn("taskB", "turnB");
    useTurnStore.getState().setTurnsForTask("taskA", [makeTurn("taskA", "turnA")]);
    const streamingBefore = streaming();
    useTurnStore.getState().updateTurn("taskA", "turnA", { status: "failed" });
    expect(streaming()).toBe(streamingBefore);
    expect(streaming()).toEqual({ taskA: "turnA", taskB: "turnB" });
  });

  it("写入使用不可变更新：旧快照对象不被就地修改", () => {
    // 测试目的：不可变性是 zustand 正确触发订阅的前提；
    // 可能发现缺陷：就地 mutate（state.streamingTurnIds[taskId]=...）导致组件不重渲染，
    //   流式指示器不出现——症状与 H1 原 bug 极为相似且更难定位。
    useTurnStore.getState().setStreamingTurn("taskA", "turnA");
    const snapshot = streaming();
    useTurnStore.getState().setStreamingTurn("taskB", "turnB");
    // 旧快照必须保持原样（只含 taskA），且引用已更换。
    expect(snapshot).toEqual({ taskA: "turnA" });
    expect(streaming()).not.toBe(snapshot);
  });
});
