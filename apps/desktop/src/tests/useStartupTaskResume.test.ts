// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

// mock api 模块：useStartupTaskResume 经 useTask.openTask 间接调用 getTask / listTaskTurns /
// listTaskEvents，经 loadWorkspaceTasks 调用 listWorkspaceTasks。全部替换为可控桩，
// 以验证「恢复时 api.getTask 仅被 openTask 内部调用一次（hook 不前置探测）」。
vi.mock("@/services/api", () => ({
  getTask: vi.fn(),
  listTaskTurns: vi.fn(),
  listTaskEvents: vi.fn(),
  listWorkspaceTasks: vi.fn(),
}));

// mock logger 以屏蔽噪声并断言日志调用。
vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  logDebug: vi.fn(),
}));

import * as api from "@/services/api";
import { useStartupTaskResume } from "@/hooks/useStartupTaskResume";
import { useTaskStore } from "@/stores/taskStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";
import { SSEConnectionState } from "@/services/sse";
import { ServiceError } from "@/services/types";
import { logError, logWarn } from "@/lib/logger";
import type { TaskRecord } from "@shared/task";
import type { TurnRecord } from "@shared/turn";
import type { RuntimeEvent } from "@shared/events";
import type { WorkspaceRecord } from "@shared/workspace";

const ACTIVE_TASK_STORAGE_KEY = "coding-agent.activeTaskId";

const WS_1 = "ws-1";
const WS_2 = "ws-2";
const TASK_1 = "task-1";
const TASK_2 = "task-2";
const TASK_404 = "task-404";

function makeWorkspace(id: string): WorkspaceRecord {
  return { workspace_id: id } as unknown as WorkspaceRecord;
}

function makeTask(id: string, workspaceId: string): TaskRecord {
  return {
    task_id: id,
    workspace_id: workspaceId,
    agent_id: "dev",
    input_text: "hi",
    title: "hi",
    last_message_preview: "hi",
    latest_turn_id: "turn-1",
    status: "completed",
    execution_status: "completed",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TaskRecord;
}

function makeTurn(taskId: string): TurnRecord {
  return {
    turn_id: "turn-1",
    task_id: taskId,
    input_text: "hi",
    status: "completed",
    end_reason: "done",
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TurnRecord;
}

function makeEvent(taskId: string, eventId: string): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: taskId,
    turn_id: "turn-1",
    event_type: "model_output_delta",
    sequence: 1,
    created_at: new Date(1000).toISOString(),
    payload: { text: eventId },
  } as unknown as RuntimeEvent;
}

/** 将某 task 的 getTask / listTaskTurns / listTaskEvents 配置为「成功返回」。 */
function stubOpenTaskSuccess(taskId: string, workspaceId: string) {
  vi.mocked(api.getTask).mockResolvedValue(makeTask(taskId, workspaceId));
  vi.mocked(api.listTaskTurns).mockResolvedValue([makeTurn(taskId)]);
  vi.mocked(api.listTaskEvents).mockResolvedValue([makeEvent(taskId, `e-${taskId}`)]);
}

/** 配置默认 workspace 的 listWorkspaceTasks 返回指定任务列表。 */
function stubWorkspaceTasks(workspaceId: string, taskIds: string[]) {
  vi.mocked(api.listWorkspaceTasks).mockResolvedValue(
    taskIds.map((id) => makeTask(id, workspaceId)),
  );
}

/** 重置全部 store 与 mock 状态。 */
function resetAll() {
  localStorage.clear();
  vi.clearAllMocks();
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
  });
  useEventStore.setState({
    events: [],
    eventsByTaskId: {},
    eventsByTurnId: {},
    connectionState: SSEConnectionState.IDLE,
    processedEventIds: new Set<string>(),
  });
  useTurnStore.setState({ turnsByTaskId: {} });
  useWorkspaceStore.setState({
    workspaces: [],
    activeWorkspaceId: null,
    collapsedWorkspaceIds: new Set<string>(),
  });
}

describe("useStartupTaskResume 启动恢复", () => {
  beforeEach(() => {
    resetAll();
  });

  it("有持久化 task 且属于第一个 workspace：启动打开该 task 并回填 events", async () => {
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_1);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1), makeWorkspace(WS_2)]);
    stubOpenTaskSuccess(TASK_1, WS_1);

    renderHook(() => useStartupTaskResume());

    await waitFor(() => {
      expect(useTaskStore.getState().activeTaskId).toBe(TASK_1);
    });
    expect(api.getTask).toHaveBeenCalledWith(TASK_1);
    expect(api.listTaskEvents).toHaveBeenCalledWith(TASK_1);
    expect(useEventStore.getState().eventsByTaskId[TASK_1]?.length).toBeGreaterThan(0);
    // 对齐到持久化 task 的 workspace（第一个）。
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe(WS_1);
  });

  it("有持久化 task 且属于非第一个 workspace：仍打开该 task 并对齐 activeWorkspaceId", async () => {
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_2);
    // 第一个 workspace 是 ws-1，持久化 task 属于 ws-2（非第一个）。
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1), makeWorkspace(WS_2)]);
    stubOpenTaskSuccess(TASK_2, WS_2);

    renderHook(() => useStartupTaskResume());

    await waitFor(() => {
      expect(useTaskStore.getState().activeTaskId).toBe(TASK_2);
    });
    expect(api.getTask).toHaveBeenCalledWith(TASK_2);
    // 不回退首个 workspace task，且 activeWorkspaceId 对齐到持久化 task 的 workspace。
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe(WS_2);
  });

  it("持久化 task 404：清理持久化并回退默认 workspace 首条 task", async () => {
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_404);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    // openTask 拉取持久化 task 时 getTask 抛 404。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("not found", { statusCode: 404 }));
    // 回退路径需 listWorkspaceTasks 成功返回首条 task，再 openTask 成功。
    stubWorkspaceTasks(WS_1, [TASK_1]);
    vi.mocked(api.getTask).mockResolvedValueOnce(makeTask(TASK_1, WS_1));
    vi.mocked(api.listTaskTurns).mockResolvedValueOnce([makeTurn(TASK_1)]);
    vi.mocked(api.listTaskEvents).mockResolvedValueOnce([makeEvent(TASK_1, "e-1")]);

    renderHook(() => useStartupTaskResume());

    // 回退后打开默认 workspace 首条 task。
    await waitFor(() => {
      expect(useTaskStore.getState().activeTaskId).toBe(TASK_1);
    });
    expect(logWarn).toHaveBeenCalled();
    expect(api.getTask).toHaveBeenCalledWith(TASK_1);
    // 404 清理了持久化脏值（回退 openTask 成功后又持久化了 TASK_1）。
    expect(localStorage.getItem(ACTIVE_TASK_STORAGE_KEY)).toBe(TASK_1);
  });

  it("持久化 task 网络错误：不清理持久化，记录错误并降级", async () => {
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_404);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    // 网络错误（statusCode 0，非 404）。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("network down", { statusCode: 0 }));
    stubWorkspaceTasks(WS_1, [TASK_1]);
    vi.mocked(api.getTask).mockResolvedValueOnce(makeTask(TASK_1, WS_1));
    vi.mocked(api.listTaskTurns).mockResolvedValueOnce([makeTurn(TASK_1)]);
    vi.mocked(api.listTaskEvents).mockResolvedValueOnce([makeEvent(TASK_1, "e-1")]);

    renderHook(() => useStartupTaskResume());

    // 降级打开默认首条 task。
    await waitFor(() => {
      expect(useTaskStore.getState().activeTaskId).toBe(TASK_1);
    });
    // 记录了 error（非 404）。
    expect(logError).toHaveBeenCalled();
    // 未走 404 清理分支：logWarn 不应被调用。
    expect(logWarn).not.toHaveBeenCalled();
  });

  it("没有持久化 task：打开默认 workspace 首条 task", async () => {
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    stubWorkspaceTasks(WS_1, [TASK_1]);
    stubOpenTaskSuccess(TASK_1, WS_1);

    renderHook(() => useStartupTaskResume());

    await waitFor(() => {
      expect(useTaskStore.getState().activeTaskId).toBe(TASK_1);
    });
    expect(api.listWorkspaceTasks).toHaveBeenCalledWith(WS_1);
    expect(api.getTask).toHaveBeenCalledWith(TASK_1);
  });

  it("workspace 列表为空：不调用 openTask", async () => {
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_1);
    // 不设置任何 workspace。

    renderHook(() => useStartupTaskResume());
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.getTask).not.toHaveBeenCalled();
    expect(api.listWorkspaceTasks).not.toHaveBeenCalled();
  });

  it("无重复请求：恢复时 api.getTask 全程仅被调用 1 次", async () => {
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_1);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    stubOpenTaskSuccess(TASK_1, WS_1);

    renderHook(() => useStartupTaskResume());

    await waitFor(() => {
      expect(useTaskStore.getState().activeTaskId).toBe(TASK_1);
    });
    // hook 不前置探测，仅 openTask 内部发出一次 GET /tasks/{id}。
    expect(api.getTask).toHaveBeenCalledTimes(1);
  });

  it("无双重恢复：重复触发 workspaces 变化时 openTask 仅被调用一次", async () => {
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_1);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    stubOpenTaskSuccess(TASK_1, WS_1);

    const { rerender } = renderHook(() => useStartupTaskResume());

    await waitFor(() => {
      expect(useTaskStore.getState().activeTaskId).toBe(TASK_1);
    });

    // 再次触发 workspaces 引用变化（模拟列表刷新），ref 去重应阻止二次恢复。
    act(() => {
      useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1), makeWorkspace(WS_2)]);
    });
    rerender();
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.getTask).toHaveBeenCalledTimes(1);
  });

  it("网络错误：localStorage 的 activeTaskId 保留不变（不清理持久化，下次启动仍可重试）", async () => {
    // 目的：验证网络错误（statusCode 0，非 404）时绝不清理持久化。
    // 可能发现的缺陷：若实现误把「非 404」也走 setActiveTask(null)，会误清持久化，
    //   导致下次启动丢失恢复机会。
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_404);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    // 网络错误（statusCode 0，非 404）。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("network down", { statusCode: 0 }));
    // 回退路径：默认 workspace 无任务（listWorkspaceTasks 返回空），避免回退 openTask
    // 成功覆盖持久化值，从而能干净地断言「持久化未被改动」。
    stubWorkspaceTasks(WS_1, []);

    renderHook(() => useStartupTaskResume());

    // 等降级流程跑完（loadWorkspaceTasks 空返回 → resumeDefaultTask 静默返回）。
    await waitFor(() => {
      expect(api.listWorkspaceTasks).toHaveBeenCalledWith(WS_1);
    });
    await act(async () => {
      await Promise.resolve();
    });

    // 关键断言：持久化值必须保留原样（TASK_404），未被 setActiveTask(null) 清空。
    expect(localStorage.getItem(ACTIVE_TASK_STORAGE_KEY)).toBe(TASK_404);
    expect(logError).toHaveBeenCalled();
    expect(logWarn).not.toHaveBeenCalled();
    // 未发生任何 setActiveTask 导致的持久化写入（回退无任务，不会 openTask）。
    expect(api.getTask).toHaveBeenCalledTimes(1); // 仅 openTask(persisted) 内部那一次。
    expect(api.getTask).toHaveBeenCalledWith(TASK_404);
  });

  it("404：显式触发 setActiveTask(null) 清空持久化，再回退默认首条 task", async () => {
    // 目的：验证 404 分支不仅回退，还真正清空了持久化脏值（setActiveTask(null) → removeItem）。
    // 可能发现的缺陷：若 404 分支只回退却未清理持久化，下次启动仍会尝试恢复已删除的 task。
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_404);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    // openTask 拉取持久化 task 时 getTask 抛 404。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("not found", { statusCode: 404 }));
    // 回退路径：默认 workspace 无任务，回退 openTask 不会执行，因此持久化不会被「新任务」覆盖，
    // 能干净断言「清理后 activeTaskId 为 null 且 localStorage 无该键」。
    stubWorkspaceTasks(WS_1, []);

    renderHook(() => useStartupTaskResume());

    await waitFor(() => {
      expect(api.listWorkspaceTasks).toHaveBeenCalledWith(WS_1);
    });
    await act(async () => {
      await Promise.resolve();
    });

    // setActiveTask(null) → applyActiveTask → persistActiveTaskId(null) → removeItem。
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(localStorage.getItem(ACTIVE_TASK_STORAGE_KEY)).toBeNull();
    // 404 清理分支经 logWarn 记录（useStartupTaskResume 模块）；openTask 内部 catch
    // 会记一次 logError（useTask 模块），二者并存，故仅断言 logWarn 被触发即可。
    expect(logWarn).toHaveBeenCalled();
  });

  it("持久化 task 404 且回退首条 task 的 openTask 也失败：静默降级到空态，不 unhandled rejection", async () => {
    // 目的：验证「清理持久化后回退默认首条 task」这条回退路径自身失败时，
    //   resumeDefaultTask 内部已 try/catch 包裹 openTask，失败仅记 logError 并静默降级，
    //   不会向上抛出导致 unhandled rejection，也不会崩溃。
    // 可能发现的缺陷：若 resumeDefaultTask 未捕获 openTask 的 rethrow（回归前的 bug），
    //   回退失败会抛到 effect 内 async 函数，产生 unhandled rejection。
    localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, TASK_404);
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    // 第一步：openTask(持久化 TASK_404) 时 getTask 抛 404 → 触发清理持久化 + 回退。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("not found", { statusCode: 404 }));
    // 回退路径：listWorkspaceTasks 成功返回首条 task（TASK_1），但打开该首条 task 时
    // getTask 再次抛 404（回退 openTask 也失败）。
    stubWorkspaceTasks(WS_1, [TASK_1]);
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("not found", { statusCode: 404 }));

    renderHook(() => useStartupTaskResume());

    // 等回退流程跑完：listWorkspaceTasks 被调用，且回退 openTask 也被触发（getTask 第 2 次）。
    await waitFor(() => {
      expect(api.getTask).toHaveBeenCalledTimes(2);
    });
    await act(async () => {
      await Promise.resolve();
    });

    // 回退失败：不崩溃、activeTaskId 保持空态，未选中任何任务。
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    // 404 清理持久化已生效：持久化值被清空。
    expect(localStorage.getItem(ACTIVE_TASK_STORAGE_KEY)).toBeNull();
    // 回退 openTask 失败已记录 error（含 from_persisted:false 上下文）。
    expect(logError).toHaveBeenCalled();
    // 无 unhandled rejection：若抛到 async 顶层，vitest 会捕获为未处理 rejection 导致测试失败。
  });

  it("无持久化 task 且默认首条 task 的 openTask 失败：静默降级，不崩溃", async () => {
    // 目的：验证「无持久化 → 打开默认 workspace 首条 task」路径中，首条 task 的 openTask
    //   失败时同样被 resumeDefaultTask 内部捕获并静默降级到空态。
    // 可能发现的缺陷：若无持久化分支同样缺少对 openTask 失败的捕获，会产生 unhandled rejection。
    // 不设置 localStorage 持久化值（冷启动）。
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace(WS_1)]);
    stubWorkspaceTasks(WS_1, [TASK_1]);
    // 打开首条 task 时 getTask 抛网络错误（statusCode 0，非 404）。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("network down", { statusCode: 0 }));

    renderHook(() => useStartupTaskResume());

    await waitFor(() => {
      expect(api.getTask).toHaveBeenCalledTimes(1);
    });
    await act(async () => {
      await Promise.resolve();
    });

    // 静默降级：activeTaskId 保持空态，未崩溃。
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    // 回退 openTask 失败已记录 error（含 from_persisted:false 上下文）。
    expect(logError).toHaveBeenCalled();
    // 无持久化分支不会走 404 清理，logWarn 不应被触发。
    expect(logWarn).not.toHaveBeenCalled();
  });
});
