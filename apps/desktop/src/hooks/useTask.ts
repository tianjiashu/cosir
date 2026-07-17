/**
 * 任务 CRUD 操作 Hook。
 *
 * 封装任务创建、查询、取消等操作，
 * 自动同步结果到 taskStore，并管理操作中的加载与错误状态。
 *
 * @module hooks/useTask
 */

import { useCallback, useState } from "react";
import { useTaskStore } from "../stores/taskStore";
import { useEventStore } from "../stores/eventStore";
import { useTurnStore } from "../stores/turnStore";
import { useSSE } from "./useSSE";
import * as api from "../services/api";
import { logError } from "../lib/logger";
import { beginClientTrace, endClientTrace, hasClientTrace } from "../services/tracePropagation";

/** 任务操作的加载状态。 */
interface TaskOperationState {
  /** 是否正在执行创建/取消等操作。 */
  loading: boolean;
  /** 上一次操作错误（如有）。 */
  error: string | null;
}

/**
 * 任务 CRUD Hook 返回值接口。
 */
interface UseTaskReturn {
  /** 在指定工作区创建新任务并自动监听首个 turn 的 SSE 流。 */
  createTask: (text: string, workspaceId: string) => Promise<void>;
  /** 给当前任务追加一个新轮次并自动监听该轮次 SSE 流。 */
  createTurn: (text: string) => Promise<void>;
  /** 加载任务历史事件和轮次，并切换为活跃任务。 */
  openTask: (taskId: string) => Promise<void>;
  /** 取消当前活跃任务。 */
  cancelTask: () => Promise<void>;
  /** 刷新当前活跃任务的最新状态。 */
  refreshTask: () => Promise<void>;
  /** 操作状态。 */
  operation: TaskOperationState;
}

/**
 * 任务 CRUD Hook。
 *
 * 提供创建任务、追加 turn、打开任务、取消和刷新任务的能力。
 * 创建或追加 turn 后通过 useSSE 连接 `/turns/{turn_id}/stream`。
 * 状态变更同步到 taskStore、turnStore 与 eventStore。
 *
 * @example
 * ```tsx
 * const { createTask, cancelTask, operation } = useTask();
 *
 * await createTask("帮我写一个登录页面", activeWorkspaceId);
 * if (operation.loading) <Spinner />;
 * ```
 */
export function useTask(): UseTaskReturn {
  const [operation, setOperation] = useState<TaskOperationState>({
    loading: false,
    error: null,
  });

  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  const addTask = useTaskStore((s) => s.addTask);
  const replaceTask = useTaskStore((s) => s.replaceTask);
  const removeTask = useTaskStore((s) => s.removeTask);
  const updateTask = useTaskStore((s) => s.updateTask);
  const setActiveTask = useTaskStore((s) => s.setActiveTask);
  const setActiveTurn = useTaskStore((s) => s.setActiveTurn);
  const setEvents = useEventStore((s) => s.setEvents);
  const setTurnsForTask = useTurnStore((s) => s.setTurnsForTask);
  const upsertTurn = useTurnStore((s) => s.upsertTurn);
  const setStreamingTurn = useTurnStore((s) => s.setStreamingTurn);
  const { connect, disconnect } = useSSE();

  /**
   * 在工作区内创建新任务并启动首个 turn 的 SSE 监听。
   *
   * @param text - 用户输入的任务文本。
   * @param workspaceId - 必填的工作区 ID。
   *
   * @sideeffect
   * - POST /workspaces/{workspace_id}/tasks 创建任务与首个 turn
   * - 更新 taskStore.tasks 和 activeTaskId
   * - 建立 `/turns/{turn_id}/stream` SSE 连接接收首个 turn 事件流
   */
  const createTask = useCallback(
    async (text: string, workspaceId: string): Promise<void> => {
      setOperation({ loading: true, error: null });
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace();
      }

      try {
        // 断开旧的 SSE 连接
        disconnect();

        const temporaryTaskId = `temp-${Date.now()}`;
        const now = new Date().toISOString();
        addTask({
          task_id: temporaryTaskId,
          workspace_id: workspaceId,
          agent_id: "developer",
          input_text: text,
          title: text.slice(0, 80),
          last_message_preview: text.slice(0, 80),
          latest_turn_id: null,
          status: "pending",
          created_at: now,
          updated_at: now,
        });
        setActiveTask(temporaryTaskId, null);

        // 调用 API 创建任务
        const task = await api.createTask({ text, workspace_id: workspaceId });

        // 同步到 store
        replaceTask(temporaryTaskId, task);
        setActiveTask(task.task_id, task.latest_turn_id);

        // 建立 SSE 连接开始接收事件流
        if (task.latest_turn_id) {
          setStreamingTurn(task.latest_turn_id);
          await connect(task.task_id, task.latest_turn_id);
        }

        setOperation({ loading: false, error: null });
      } catch (err) {
        const message = err instanceof Error ? err.message : "创建任务失败";
        logError("createTask 失败", err, { module: "useTask" });
        const temporaryTask = useTaskStore.getState().tasks.find((task) => task.task_id.startsWith("temp-"));
        if (temporaryTask) {
          removeTask(temporaryTask.task_id);
        }
        setOperation({ loading: false, error: message });
      } finally {
        if (ownsOperation) {
          endClientTrace();
        }
      }
    },
    [addTask, replaceTask, removeTask, setActiveTask, connect, disconnect, setStreamingTurn],
  );

  /**
   * 给当前活跃任务追加新轮次并启动 turn 级 SSE。
   *
   * @param text - 本轮用户输入文本。
   *
   * @sideeffect
   * - POST /tasks/{task_id}/turns 创建 pending turn
   * - 更新 taskStore.activeTurnId 和 turnStore
   * - 连接 /turns/{turn_id}/stream
   */
  const createTurn = useCallback(
    async (text: string): Promise<void> => {
      if (!activeTaskId) {
        setOperation({ loading: false, error: "未选择任务" });
        return;
      }
      setOperation({ loading: true, error: null });
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace({ taskId: activeTaskId });
      }

      try {
        disconnect();
        const turn = await api.createTaskTurn(activeTaskId, { input_text: text });
        upsertTurn(turn);
        setActiveTurn(turn.turn_id);
        updateTask(activeTaskId, {
          latest_turn_id: turn.turn_id,
          last_message_preview: text.slice(0, 80),
          status: "pending",
        });
        setStreamingTurn(turn.turn_id);
        await connect(activeTaskId, turn.turn_id);
        setOperation({ loading: false, error: null });
      } catch (err) {
        const message = err instanceof Error ? err.message : "创建轮次失败";
        logError("createTurn 失败", err, { module: "useTask", task_id: activeTaskId });
        setOperation({ loading: false, error: message });
      } finally {
        if (ownsOperation) {
          endClientTrace();
        }
      }
    },
    [activeTaskId, connect, disconnect, setActiveTurn, setStreamingTurn, updateTask, upsertTurn],
  );

  /**
   * 加载任务历史并切换当前任务。
   *
   * @param taskId - 待打开的任务标识。
   *
   * @sideeffect 从后端读取 events/turns 并写入对应 store。
   */
  const openTask = useCallback(
    async (taskId: string): Promise<void> => {
      setOperation({ loading: true, error: null });
      try {
        const [events, turns] = await Promise.all([
          api.getTaskEvents(taskId),
          api.listTaskTurns(taskId),
        ]);
        setEvents(events, taskId);
        setTurnsForTask(taskId, turns);
        setActiveTask(taskId, turns.length > 0 ? turns[turns.length - 1].turn_id : null);
        setOperation({ loading: false, error: null });
      } catch (err) {
        const message = err instanceof Error ? err.message : "打开任务失败";
        logError("openTask 失败", err, { module: "useTask", task_id: taskId });
        setOperation({ loading: false, error: message });
      }
    },
    [setActiveTask, setEvents, setTurnsForTask],
  );

  /**
   * 取消当前活跃任务。
   *
   * @sideeffect POST /tasks/{id}/cancel + 更新 store 状态。
   */
  const cancelTask = useCallback(async (): Promise<void> => {
    if (!activeTaskId) return;

    setOperation({ loading: true, error: null });
    const ownsOperation = !hasClientTrace();
    if (ownsOperation) {
      beginClientTrace({ taskId: activeTaskId });
    }

    try {
      const updated = await api.cancelTask(activeTaskId);
      updateTask(activeTaskId, { status: updated.status });

      // 断开 SSE 连接（任务已取消）
      disconnect();

      setOperation({ loading: false, error: null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "取消任务失败";
      logError("cancelTask 失败", err, { module: "useTask", task_id: activeTaskId });
      setOperation({ loading: false, error: message });
    } finally {
      if (ownsOperation) {
        endClientTrace();
      }
    }
  }, [activeTaskId, updateTask, disconnect]);

  /**
   * 从后端刷新当前活跃任务的最新状态。
   */
  const refreshTask = useCallback(async (): Promise<void> => {
    if (!activeTaskId) return;
    const ownsOperation = !hasClientTrace();
    if (ownsOperation) {
      beginClientTrace({ taskId: activeTaskId });
    }

    try {
      const task = await api.getTask(activeTaskId);
      updateTask(activeTaskId, { status: task.status, updated_at: task.updated_at });
    } catch (err) {
      logError("refreshTask 失败", err, { module: "useTask", task_id: activeTaskId });
    } finally {
      if (ownsOperation) {
        endClientTrace();
      }
    }
  }, [activeTaskId, updateTask]);

  return { createTask, createTurn, openTask, cancelTask, refreshTask, operation };
}
