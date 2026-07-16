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
  /** 创建一个新任务并自动开始监听其 SSE 流。 */
  createTask: (text: string, sessionId?: string) => Promise<void>;
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
 * 提供创建、取消、刷新任务的能力。
 * 创建任务后自动通过 useSSE 连接事件流。
 * 所有状态变更自动同步到 taskStore / eventStore。
 *
 * @example
 * ```tsx
 * const { createTask, cancelTask, operation } = useTask();
 *
 * await createTask("帮我写一个登录页面");
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
  const updateTask = useTaskStore((s) => s.updateTask);
  const setActiveTask = useTaskStore((s) => s.setActiveTask);
  const clearEvents = useEventStore((s) => s.clearEvents);
  const { connect, disconnect } = useSSE();

  /**
   * 创建新任务并启动 SSE 监听。
   *
   * @param text - 用户输入的任务文本。
   * @param sessionId - 可选的会话 ID。
   *
   * @sideeffect
   * - POST /tasks 创建后端任务记录
   * - 更新 taskStore.tasks 和 activeTaskId
   * - 清空 eventStore 旧事件
   * - 建立 SSE 连接开始接收事件流
   */
  const createTask = useCallback(
    async (text: string, sessionId?: string): Promise<void> => {
      setOperation({ loading: true, error: null });
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace();
      }

      try {
        // 断开旧的 SSE 连接
        disconnect();

        // 调用 API 创建任务
        const task = await api.createTask({ text, session_id: sessionId });

        // 同步到 store
        addTask(task);
        setActiveTask(task.task_id);
        clearEvents();

        // 建立 SSE 连接开始接收事件流
        await connect(task.task_id);

        setOperation({ loading: false, error: null });
      } catch (err) {
        const message = err instanceof Error ? err.message : "创建任务失败";
        logError("createTask 失败", err, { module: "useTask" });
        setOperation({ loading: false, error: message });
      } finally {
        if (ownsOperation) {
          endClientTrace();
        }
      }
    },
    [addTask, setActiveTask, clearEvents, connect, disconnect],
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

  return { createTask, cancelTask, refreshTask, operation };
}
