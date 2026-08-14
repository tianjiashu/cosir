// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

// mock api 模块：openTask 通过 api.getTask / listTaskTurns / listTaskEvents 拉取数据，
// 这里全部替换为本测试可控的桩，重点验证「缓存命中时是否跳过 listTaskEvents」。
vi.mock("@/services/api", () => ({
  getTask: vi.fn(),
  listTaskTurns: vi.fn(),
  listTaskEvents: vi.fn(),
}));

import * as api from "@/services/api";
import { useTask } from "@/hooks/useTask";
import { useEventStore } from "@/stores/eventStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { SSEConnectionState } from "@/services/sse";
import { ServiceError } from "@/services/types";
import type { RuntimeEvent } from "@shared/events";
import type { TaskRecord } from "@shared/task";
import type { TurnRecord } from "@shared/turn";

const TASK_ID = "task-force-refresh";

function makeEvent(eventId: string, sequence: number): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: TASK_ID,
    turn_id: "turn-1",
    event_type: "model_output_delta",
    sequence,
    created_at: new Date(sequence * 1000).toISOString(),
    payload: { text: `t-${eventId}` },
  } as unknown as RuntimeEvent;
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

describe("useTask.openTask forceRefresh 行为", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStores();
    vi.mocked(api.getTask).mockResolvedValue({
      task_id: TASK_ID,
      workspace_id: "ws",
      agent_id: "dev",
      input_text: "hi",
      title: "hi",
      last_message_preview: "hi",
      latest_turn_id: "turn-1",
      status: "completed",
      execution_status: "completed",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    } as unknown as TaskRecord);
    vi.mocked(api.listTaskTurns).mockResolvedValue([
      {
        turn_id: "turn-1",
        task_id: TASK_ID,
        input_text: "hi",
        status: "completed",
        end_reason: "done",
        response_text: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      } as unknown as TurnRecord,
    ] as unknown as TurnRecord[]);
    vi.mocked(api.listTaskEvents).mockResolvedValue([makeEvent("e1", 1)]);
  });

  it("缓存命中时跳过 listTaskEvents（forceRefresh 默认 false）", async () => {
    // 预先灌入该 task 的历史缓存，模拟「本会话已加载过」。
    useEventStore.getState().setEvents([makeEvent("cached-e1", 1)], TASK_ID);

    const { result } = renderHook(() => useTask());
    await act(async () => {
      await result.current.openTask(TASK_ID);
    });

    // 缓存非空 → 不应再请求历史事件。
    expect(api.listTaskEvents).not.toHaveBeenCalled();
  });

  it("缓存为空时仍走 listTaskEvents（冷启动）", async () => {
    const { result } = renderHook(() => useTask());
    await act(async () => {
      await result.current.openTask(TASK_ID);
    });

    expect(api.listTaskEvents).toHaveBeenCalledTimes(1);
    expect(api.listTaskEvents).toHaveBeenCalledWith(TASK_ID);
  });

  it("forceRefresh=true 时即使有缓存也重新拉取", async () => {
    useEventStore.getState().setEvents([makeEvent("cached-e1", 1)], TASK_ID);

    const { result } = renderHook(() => useTask());
    await act(async () => {
      await result.current.openTask(TASK_ID, true);
    });

    // 强制刷新 → 忽略缓存，重新拉取历史事件。
    expect(api.listTaskEvents).toHaveBeenCalledTimes(1);
    expect(api.listTaskEvents).toHaveBeenCalledWith(TASK_ID);
  });

  it("对列表首项调用 openTask 会选中该任务并加载历史（首屏回放契约）", async () => {
    // 模拟「首屏 setTasks 回退选中列表首项」后，App 对该首项调 openTask 的场景：
    // 必须真正切换到该任务（activeTaskId）并拉取历史事件，而非留空白。
    const FIRST_TASK_ID = "task-first-item";

    vi.mocked(api.getTask).mockResolvedValueOnce({
      task_id: FIRST_TASK_ID,
      workspace_id: "ws",
      agent_id: "dev",
      input_text: "hi",
      title: "hi",
      last_message_preview: "hi",
      latest_turn_id: "turn-1",
      status: "completed",
      execution_status: "completed",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    } as unknown as TaskRecord);
    vi.mocked(api.listTaskTurns).mockResolvedValueOnce([
      {
        turn_id: "turn-1",
        task_id: FIRST_TASK_ID,
        input_text: "hi",
        status: "completed",
        end_reason: "done",
        response_text: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      } as unknown as TurnRecord,
    ] as unknown as TurnRecord[]);
    vi.mocked(api.listTaskEvents).mockResolvedValueOnce([makeEvent("e1", 1)]);

    const { result } = renderHook(() => useTask());
    await act(async () => {
      await result.current.openTask(FIRST_TASK_ID);
    });

    // 选中首项：activeTaskId 应切到该任务，避免中央会话区空白。
    expect(useTaskStore.getState().activeTaskId).toBe(FIRST_TASK_ID);
    // 回放历史：事件流被拉取并缓存到该 task。
    expect(api.listTaskEvents).toHaveBeenCalledWith(FIRST_TASK_ID);
    expect(useEventStore.getState().eventsByTaskId[FIRST_TASK_ID]?.length).toBeGreaterThan(0);
  });
});

describe("useTask.openTask rethrow 回归", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStores();
  });

  it("getTask 抛 404 ServiceError：openTask 应向外 reject，而非吞掉", async () => {
    // 目的：验证 openTask 对「拉取 task/turns 失败」会 rethrow 原始错误，
    //   使调用方（启动恢复 hook）能据 statusCode 区分 404 与网络错误。
    // 可能发现的缺陷：若 openTask 内部吞掉错误只 setOperation，调用方将无法区分 404，
    //   启动恢复无法清理持久化脏值。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("not found", { statusCode: 404 }));

    const { result } = renderHook(() => useTask());
    let caught: unknown = "not-thrown";
    await act(async () => {
      try {
        await result.current.openTask(TASK_ID);
      } catch (err) {
        caught = err;
      }
    });

    expect(caught).toBeInstanceOf(ServiceError);
    expect((caught as ServiceError).statusCode).toBe(404);
  });

  it("getTask 抛网络错误（非 404）：openTask 应向外 reject，而非吞掉", async () => {
    // 目的：验证网络错误同样 rethrow（保持 statusCode=0 可被调用方识别为非 404）。
    // 可能发现的缺陷：若只对 404 rethrow、对其它错误吞掉，调用方无法触发「保留持久化降级」分支。
    vi.mocked(api.getTask).mockRejectedValueOnce(new ServiceError("network down", { statusCode: 0 }));

    const { result } = renderHook(() => useTask());
    let caught: unknown = "not-thrown";
    await act(async () => {
      try {
        await result.current.openTask(TASK_ID);
      } catch (err) {
        caught = err;
      }
    });

    expect(caught).toBeInstanceOf(ServiceError);
    expect((caught as ServiceError).statusCode).toBe(0);
  });

  it("listTaskTurns 抛错：openTask 同样 rethrow（Promise.all 任一 reject 即整体 reject）", async () => {
    // 目的：验证 turns 拉取失败也走同一 rethrow 路径，调用方统一处理。
    // 可能发现的缺陷：若 turns 失败被单独吞掉，任务/turns 骨架未就绪却继续渲染，属契约违背。
    vi.mocked(api.getTask).mockResolvedValue({
      task_id: TASK_ID,
      workspace_id: "ws",
    } as unknown as TaskRecord);
    vi.mocked(api.listTaskTurns).mockRejectedValueOnce(new ServiceError("boom", { statusCode: 500 }));

    const { result } = renderHook(() => useTask());
    let caught: unknown = "not-thrown";
    await act(async () => {
      try {
        await result.current.openTask(TASK_ID);
      } catch (err) {
        caught = err;
      }
    });

    expect(caught).toBeInstanceOf(ServiceError);
    expect((caught as ServiceError).statusCode).toBe(500);
  });

  it("历史事件回填失败不 rethrow：openTask 仍正常 resolve（非致命分支被单独捕获）", async () => {
    // 目的：验证事件回填失败属非致命、被内部捕获，openTask 不因此 reject，
    //   否则启动恢复 hook 会把「事件拉取失败」误判为任务不存在/网络错误而走错误降级。
    // 可能发现的缺陷：若事件失败被错误地 rethrow，会把非致命错误升级为致命，破坏降级语义。
    vi.mocked(api.listTaskEvents).mockRejectedValueOnce(new ServiceError("events boom", { statusCode: 0 }));

    const { result } = renderHook(() => useTask());
    await act(async () => {
      await result.current.openTask(TASK_ID);
    });

    // 正常 resolve，且仍完成选中。
    expect(useTaskStore.getState().activeTaskId).toBe(TASK_ID);
    // 事件错误仅标记 eventsError，不污染 task/turns 的加载结果。
    expect(result.current.operation.eventsError).not.toBeNull();
  });
});
