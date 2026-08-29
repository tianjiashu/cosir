/**
 * 修复验证测试：缺陷 2（P0）- 草稿迁移后立刻自删
 *
 * 覆盖点：
 *  1. replaceTask(oldTaskId, realTask) 后，内存 drafts[realTask.task_id] 能读到草稿，
 *     且 drafts[oldTaskId] 已清除
 *  2. localStorage（coding-agent.inputDrafts）中草稿挂在真实 task_id 上，未被删除
 *     也未残留在占位键上
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class MemoryStorage {
  private map = new Map<string, string>();
  getItem(key: string): string | null {
    return this.map.has(key) ? (this.map.get(key) as string) : null;
  }
  setItem(key: string, value: string): void {
    this.map.set(key, String(value));
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
  clear(): void {
    this.map.clear();
  }
  get length(): number {
    return this.map.size;
  }
  key(index: number): string | null {
    return Array.from(this.map.keys())[index] ?? null;
  }
}

const memoryStorage = new MemoryStorage();
vi.stubGlobal("localStorage", memoryStorage);

vi.mock("@/lib/logger", () => ({
  logError: vi.fn(),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
}));
vi.mock("@/stores/eventStore", () => {
  const useEventStore = () => ({ invalidateTask: vi.fn() });
  (useEventStore as any).getState = () => ({ invalidateTask: vi.fn() });
  return { useEventStore };
});
// replaceTask 持久化草稿经由 listModels 之外的 services/api，但其内部直接使用
// localStorage，无需 mock api。selectedModel 持久化用 persistSelectedModel，不涉及 api。

import { useTaskStore } from "@/stores/taskStore";
import type { TaskRecord } from "@shared/task";

const INPUT_DRAFTS_KEY = "coding-agent.inputDrafts";

function makeTask(task_id: number, workspace_id = 100): TaskRecord {
  return {
    task_id,
    workspace_id,
    title: "t",
    status: "pending",
    execution_status: "pending",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

function readDrafts(): Record<string, string> {
  const raw = memoryStorage.getItem(INPUT_DRAFTS_KEY);
  return raw ? (JSON.parse(raw) as Record<string, string>) : {};
}

describe("缺陷2: replaceTask 草稿迁移不应自删", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    memoryStorage.clear();
    // 重置 store 草稿为真实 task / 占位 task 的设定
    useTaskStore.setState({ drafts: {}, tasksById: {}, tasksByWorkspaceId: {}, loadedWorkspaceIds: new Set() });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("replaceTask 后内存 drafts[真实task_id] 读到草稿，drafts[占位oldTaskId] 已清除", () => {
    const oldTaskId = -1;
    const realTaskId = 555;
    // 模拟乐观期写入占位任务的草稿
    useTaskStore.getState().setInputDraft(oldTaskId, "未发送的草稿内容");
    // 占位任务也已进 tasksById（replaceTask 要按 oldTaskId 删除）
    useTaskStore.setState((s) => ({
      tasksById: { ...s.tasksById, [oldTaskId]: makeTask(oldTaskId) },
    }));

    useTaskStore.getState().replaceTask(oldTaskId, makeTask(realTaskId));

    const drafts = useTaskStore.getState().drafts;
    expect(drafts[realTaskId]).toBe("未发送的草稿内容");
    expect(drafts[oldTaskId]).toBeUndefined();
  });

  it("localStorage 中草稿挂在真实 task_id 上，占位键被清除（无残留、无删除）", () => {
    const oldTaskId = -2;
    const realTaskId = 777;
    useTaskStore.getState().setInputDraft(oldTaskId, "草稿A");
    useTaskStore.setState((s) => ({
      tasksById: { ...s.tasksById, [oldTaskId]: makeTask(oldTaskId) },
    }));

    useTaskStore.getState().replaceTask(oldTaskId, makeTask(realTaskId));
    // 推进 debounce 计时器，使 persistInputDraft 的异步写入落盘
    vi.advanceTimersByTime(400);

    const persisted = readDrafts();
    expect(persisted[String(realTaskId)]).toBe("草稿A");
    expect(persisted[String(oldTaskId)]).toBeUndefined();
  });

  it("占位任务无草稿时 replaceTask 不应在真实 task_id 上写入空条目", () => {
    const oldTaskId = -3;
    const realTaskId = 888;
    useTaskStore.setState((s) => ({
      tasksById: { ...s.tasksById, [oldTaskId]: makeTask(oldTaskId) },
      // drafts 不含 oldTaskId
    }));

    useTaskStore.getState().replaceTask(oldTaskId, makeTask(realTaskId));
    vi.advanceTimersByTime(400);

    const persisted = readDrafts();
    expect(persisted[String(realTaskId)]).toBeUndefined();
    expect(persisted[String(oldTaskId)]).toBeUndefined();
  });

  it("已加载分组的 workspace 中 replaceTask 会把真实 task 注入分组", () => {
    const oldTaskId = -4;
    const realTaskId = 999;
    const wsId = 200;
    useTaskStore.setState((s) => ({
      tasksById: { ...s.tasksById, [oldTaskId]: makeTask(oldTaskId, wsId) },
      loadedWorkspaceIds: new Set([wsId]),
      tasksByWorkspaceId: { [wsId]: [] },
    }));

    useTaskStore.getState().replaceTask(oldTaskId, makeTask(realTaskId, wsId));
    const group = useTaskStore.getState().tasksByWorkspaceId[wsId];
    expect(group.some((t) => t.task_id === realTaskId)).toBe(true);
    expect(group.some((t) => t.task_id === oldTaskId)).toBe(false);
  });
});
