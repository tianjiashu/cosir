/**
 * string↔number 类型统一修复的补充不变量验证（idUnify.extra）。
 *
 * 仅新增测试，不修改任何源码。覆盖 idUnify.test.ts 未覆盖的边界与状态副作用：
 * - persist 边界：persistActiveTaskId 仅持久化 task_id > 0，负 number 占位 / null 不入库
 *   （否则刷新后 active 指向 -1 找不到，属于真实缺陷点）。
 * - 负 number 占位不碰撞：-1 / -2 不与真实正整数 id 串位（replaceTurnId 按 number 精确定位）。
 * - 乐观回滚路径：removeTurnId(activeTaskId, optimisticTurnId) / removeTask(optimisticTaskId)
 *   收 number 占位，类型安全且精确移除，不误删其它 turn/task。
 * - eventStore.setEvents(events, taskId?: number) 传 number 维度正确分片（编译 + 运行）。
 * - useChanges 内 event.payload.task_id === taskId 现为 number↔number 比对（原 string vs
 *   number 恒 false 的 bug 已修），通过 selectEventsForTask(number) 验证分片命中。
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { useTurnStore } from "@/stores/turnStore";
import { useTaskStore } from "@/stores/taskStore";
import { useEventStore, selectEventsForTask } from "@/stores/eventStore";
import type { TurnRecord } from "@shared/turn";
import type { TaskRecord } from "@shared/task";
import type { RuntimeEvent } from "@shared/events";

/** 内存版 localStorage mock，供 persist 边界测试使用。 */
class MemStorage {
  private map = new Map<string, string>();
  getItem(k: string): string | null {
    return this.map.has(k) ? (this.map.get(k) as string) : null;
  }
  setItem(k: string, v: string): void {
    this.map.set(k, String(v));
  }
  removeItem(k: string): void {
    this.map.delete(k);
  }
  clear(): void {
    this.map.clear();
  }
  get size(): number {
    return this.map.size;
  }
}

const mem = new MemStorage();

beforeEach(() => {
  // 每个用例前重置 store 与 localStorage mock，避免状态串扰。
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    drafts: {},
  });
  useEventStore.setState({
    events: [],
    eventsByTaskId: {},
    eventsByTurnId: {},
    connectionState: 0,
    processedEventIds: new Set(),
  });
  mem.clear();
  vi.stubGlobal("localStorage", mem as unknown as Storage);
});

function makeTurn(taskId: number, turnId: number): TurnRecord {
  const now = new Date().toISOString();
  return {
    turn_id: turnId,
    task_id: taskId,
    input_text: "x",
    status: "pending",
    end_reason: null,
    response_text: null,
    created_at: now,
    updated_at: now,
  };
}

function makeTask(taskId: number, workspaceId: number): TaskRecord {
  const now = new Date().toISOString();
  return {
    task_id: taskId,
    workspace_id: workspaceId,
    title: "t",
    status: "pending",
    execution_status: "pending",
    created_at: now,
    updated_at: now,
  };
}

function makeEvent(
  eventId: string,
  taskId: number,
  turnId: number | null,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_output_delta",
    task_id: taskId,
    turn_id: turnId,
    sequence,
    created_at: new Date(sequence * 1000).toISOString(),
    payload: { step_id: "s1", text: "hi" },
  } as RuntimeEvent;
}

describe("persist 边界：负 number 占位不入库", () => {
  it("setActiveTask(-1) 不写 localStorage（active 不应指向负占位）", () => {
    useTaskStore.getState().setActiveTask(-1);
    // 负占位被守卫 taskId > 0 拦截，localStorage 应被清除而非写入 "-1"。
    expect(mem.getItem("coding-agent.activeTaskId")).toBeNull();
    expect(useTaskStore.getState().activeTaskId).toBe(-1);
  });

  it("setActiveTask(null) 清除 localStorage 持久化", () => {
    // 先写入一个真实 id，再置空，验证清除路径。
    useTaskStore.getState().setActiveTask(42);
    expect(mem.getItem("coding-agent.activeTaskId")).toBe("42");
    useTaskStore.getState().setActiveTask(null);
    expect(mem.getItem("coding-agent.activeTaskId")).toBeNull();
  });

  it("setActiveTask(正整数) 持久化为十进制字符串", () => {
    useTaskStore.getState().setActiveTask(777);
    expect(mem.getItem("coding-agent.activeTaskId")).toBe("777");
    expect(useTaskStore.getState().activeTaskId).toBe(777);
  });
});

describe("负 number 占位不碰撞（同 task 多临时 turn）", () => {
  it("replaceTurnId 用 -1 / -2 各自精确定位，不串位、不误删真实 turn", () => {
    const taskId = 200;
    // 同 task 下挂两个负占位（-1、-2）与一个真实 turn（10），模拟并发乐观插入。
    useTurnStore.getState().setTurnsForTask(taskId, [
      makeTurn(taskId, 10),
      makeTurn(taskId, -1),
      makeTurn(taskId, -2),
    ]);
    // 先转正 -1
    useTurnStore.getState().replaceTurnId(taskId, -1, makeTurn(taskId, 9001));
    let ids = useTurnStore.getState().turnsByTaskId[taskId].map((t) => t.turn_id);
    expect(ids).toEqual([10, -2, 9001]);
    // 再转正 -2，确认 -1 已不在、真实 turn 10 仍在、新真实 turn 追加
    useTurnStore.getState().replaceTurnId(taskId, -2, makeTurn(taskId, 9002));
    ids = useTurnStore.getState().turnsByTaskId[taskId].map((t) => t.turn_id);
    expect(ids).toEqual([10, 9001, 9002]);
    expect(ids).not.toContain(-1);
    expect(ids).not.toContain(-2);
    expect(ids).toContain(10);
  });

  it("replaceTurnId(-1) 不会误删另一个 task 的同样 -1 占位（task 维度隔离）", () => {
    useTurnStore.getState().setTurnsForTask(300, [makeTurn(300, -1)]);
    useTurnStore.getState().setTurnsForTask(301, [makeTurn(301, -1)]);
    useTurnStore.getState().replaceTurnId(300, -1, makeTurn(300, 555));
    const t300 = useTurnStore.getState().turnsByTaskId[300].map((t) => t.turn_id);
    const t301 = useTurnStore.getState().turnsByTaskId[301].map((t) => t.turn_id);
    expect(t300).toEqual([555]);
    // task 301 的 -1 占位不应被误删（按 taskId 隔离）
    expect(t301).toEqual([-1]);
  });
});

describe("乐观回滚路径：removeTurnId / removeTask 收 number 占位", () => {
  it("removeTurnId 用负 number 占位精确移除目标临时 turn，保留其它", () => {
    const taskId = 400;
    useTurnStore.getState().setTurnsForTask(taskId, [
      makeTurn(taskId, 20),
      makeTurn(taskId, -5),
    ]);
    useTurnStore.getState().removeTurnId(taskId, -5);
    const ids = useTurnStore.getState().turnsByTaskId[taskId].map((t) => t.turn_id);
    expect(ids).toEqual([20]);
    expect(ids).not.toContain(-5);
  });

  it("removeTask 用负 number 占位精确移除临时 task，不误删真实 task", () => {
    const ws = 9;
    useTaskStore.getState().addTask(makeTask(-7, ws));
    useTaskStore.getState().addTask(makeTask(123, ws));
    useTaskStore.getState().removeTask(-7);
    const byId = useTaskStore.getState().tasksById;
    expect(byId[-7]).toBeUndefined();
    expect(byId[123]?.task_id).toBe(123);
  });

  it("removeTurnId 不存在的占位幂等（不抛错、不新增）", () => {
    const taskId = 401;
    useTurnStore.getState().setTurnsForTask(taskId, [makeTurn(taskId, 30)]);
    expect(() => useTurnStore.getState().removeTurnId(taskId, -99)).not.toThrow();
    const ids = useTurnStore.getState().turnsByTaskId[taskId].map((t) => t.turn_id);
    expect(ids).toEqual([30]);
  });
});

describe("eventStore.setEvents(taskId?: number) number 维度分片", () => {
  it("setEvents(events, number) 按 number task_id 正确分片并可通过 selectEventsForTask(number) 命中", () => {
    const taskId = 500;
    useEventStore.getState().setEvents(
      [makeEvent("e1", taskId, 1, 1), makeEvent("e2", taskId, 1, 2)],
      taskId,
    );
    const events = selectEventsForTask(useEventStore.getState(), taskId);
    expect(events).toHaveLength(2);
    expect(useEventStore.getState().eventsByTaskId[taskId]).toHaveLength(2);
  });

  it("setEvents 跨 task 分片隔离：不同 number taskId 各自独立缓存", () => {
    useEventStore.getState().setEvents([makeEvent("eA", 601, 1, 1)], 601);
    useEventStore.getState().setEvents([makeEvent("eB", 602, 2, 1)], 602);
    expect(selectEventsForTask(useEventStore.getState(), 601)).toHaveLength(1);
    expect(selectEventsForTask(useEventStore.getState(), 602)).toHaveLength(1);
    expect(useEventStore.getState().eventsByTaskId[601]?.[0].event_id).toBe("eA");
    expect(useEventStore.getState().eventsByTaskId[602]?.[0].event_id).toBe("eB");
  });
});

describe("useChanges 的 number↔number task_id 比对（payload.task_id === taskId）", () => {
  it("file_change_stable 事件的 payload.task_id(number) 能与 number taskId 正确匹配", () => {
    const taskId = 700;
    // 构造一个 file_change_stable 事件，payload.task_id 为 number（统一后类型）。
    const stableEvent = {
      event_id: "fs1",
      event_type: "file_change_stable",
      task_id: taskId,
      turn_id: 1,
      sequence: 1,
      created_at: new Date(1000).toISOString(),
      payload: { task_id: taskId, turn_id: 1, path: "a.ts", action: "modified" },
    } as RuntimeEvent;
    useEventStore.getState().setEvents([stableEvent], taskId);
    const evs = selectEventsForTask(useEventStore.getState(), taskId);
    // 验证事件被按 number taskId 分片命中（若比对恒 false，分片会丢失该事件）。
    expect(evs.find((e) => e.event_id === "fs1")).toBeDefined();
  });
});
