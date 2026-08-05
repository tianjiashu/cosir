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
  useTaskStore.setState({ tasks: [], activeTaskId: null, activeTurnId: null });
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
});
