// @vitest-environment happy-dom
/**
 * useTask.openTask 快速切换任务的竞态验证。
 *
 * 背景：useTask 完全没有请求竞态防护（无 isCancelled / 请求序号 / abort）。
 * openTask 里 `await api.getTask(taskId)` + `setActiveTask(taskId)` 是「先拉取后设活跃」，
 * 若用户快速连点 taskA → taskB：
 *   - taskB 的 getTask 快，先返回 → setActiveTask("taskB") ✓
 *   - taskA 的 getTask 慢，后返回 → setActiveTask("taskA") ✗ 把活跃任务从 taskB 覆盖回 taskA
 * 这是经典「过期异步响应覆盖新状态」竞态。本测试用可控 deferred 精确复现时序。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

vi.mock("@/services/api", () => ({
  getTask: vi.fn(),
  listTaskTurns: vi.fn(),
  listTaskEvents: vi.fn(),
}));
vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

import * as api from "@/services/api";
import { useTask } from "@/hooks/useTask";
import { useEventStore } from "@/stores/eventStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { SSEConnectionState } from "@/services/sse";
import type { TaskRecord } from "@shared/task";
import type { TurnRecord } from "@shared/turn";

function makeTask(id: string, status = "completed"): TaskRecord {
  return {
    task_id: id,
    workspace_id: "ws",
    agent_id: "dev",
    input_text: `task-${id}`,
    title: `task-${id}`,
    last_message_preview: `task-${id}`,
    latest_turn_id: `turn-${id}`,
    status,
    execution_status: status,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TaskRecord;
}

function makeTurn(taskId: string): TurnRecord {
  return {
    turn_id: `turn-${taskId}`,
    task_id: taskId,
    input_text: "hi",
    status: "completed",
    end_reason: null,
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TurnRecord;
}

/** 可控 deferred：外部决定 resolve 时机。 */
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

function resetStores() {
  useEventStore.setState({
    events: [],
    eventsByTaskId: {},
    eventsByTurnId: {},
    connectionState: SSEConnectionState.IDLE,
    processedEventIds: new Set<string>(),
  });
  useTaskStore.setState({ tasksByWorkspaceId: {}, activeTaskId: null, activeTurnId: null });
  useTurnStore.setState({ turnsByTaskId: {} });
}

describe("useTask.openTask 切换竞态", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStores();
  });

  it("快速切换 taskA→taskB 时，慢的 taskA 请求后返回不得把活跃任务覆盖回 taskA", async () => {
    const taskADeferred = deferred<void>();
    const taskBDeferred = deferred<void>();
    // taskA 的 getTask 慢（挂起），taskB 的快（可立即 resolve）
    const getTaskMock = vi
      .fn()
      .mockReturnValueOnce(taskADeferred.promise.then(() => makeTask("taskA")))
      .mockReturnValueOnce(taskBDeferred.promise.then(() => makeTask("taskB")));
    vi.mocked(api.getTask).mockImplementation(getTaskMock);
    vi.mocked(api.listTaskTurns).mockImplementation((taskId: string) =>
      Promise.resolve([makeTurn(taskId)]),
    );
    vi.mocked(api.listTaskEvents).mockResolvedValue([]);

    const { result } = renderHook(() => useTask());

    // 先点 taskA（慢）
    const pA = act(async () => {
      const p = result.current.openTask("taskA");
      // 让 taskA 挂起在 getTask
      await Promise.resolve();
      return p;
    });
    // 再点 taskB（快）
    const pB = act(async () => {
      result.current.openTask("taskB");
      await Promise.resolve();
    });

    // 先 resolve taskB：应设活跃为 taskB
    act(() => {
      taskBDeferred.resolve();
    });
    await pB;
    expect(useTaskStore.getState().activeTaskId).toBe("taskB");

    // 再 resolve taskA（过期响应迟到）
    act(() => {
      taskADeferred.resolve();
    });
    await pA;

    // 期望：活跃仍是 taskB，不被过期 taskA 覆盖。
    // 注：修复前此断言失败（activeTaskId 被覆盖回 taskA），证明竞态存在；修复后通过。
    expect(useTaskStore.getState().activeTaskId).toBe("taskB");
  });

  it("同一任务重复 openTask（forceRefresh）仍能正常切活跃（seq 防护不误伤）", async () => {
    vi.mocked(api.getTask).mockResolvedValue(makeTask("taskA"));
    vi.mocked(api.listTaskTurns).mockResolvedValue([makeTurn("taskA")]);
    vi.mocked(api.listTaskEvents).mockResolvedValue([]);

    const { result } = renderHook(() => useTask());
    await act(async () => {
      await result.current.openTask("taskA");
    });
    expect(useTaskStore.getState().activeTaskId).toBe("taskA");

    // 再次 openTask 同一任务（如用户点开已打开的任务，触发 forceRefresh 重拉）
    await act(async () => {
      await result.current.openTask("taskA", true);
    });
    // 防护不应阻止同一任务正常设为活跃
    expect(useTaskStore.getState().activeTaskId).toBe("taskA");
  });
});
