/**
 * 任务 CRUD 操作 Hook。
 *
 * 封装任务创建、查询、取消等操作，
 * 自动同步结果到 taskStore，并管理操作中的加载与错误状态。
 *
 * @module hooks/useTask
 */

import { useCallback, useRef, useState } from "react";
import { useTaskStore } from "../stores/taskStore";
import { loadWorkspaceTasks } from "./useWorkspaceTaskLazyLoad";
import { useEventStore } from "../stores/eventStore";
import { useTurnStore } from "../stores/turnStore";
import { useSSE } from "./useSSE";
import * as api from "../services/api";
import type { TurnRecord } from "@shared/turn";
import { logError } from "../lib/logger";
import { beginClientTrace, endClientTrace, hasClientTrace } from "../services/tracePropagation";
import { PerfTrace } from "../lib/perf";

/** 任务操作的加载状态。 */
interface TaskOperationState {
  /** 是否正在执行创建/取消等操作。 */
  loading: boolean;
  /** 上一次操作错误（如有）。 */
  error: string | null;
  /** 历史事件回填失败信息（骨架已就绪但内容未完整加载）；当前仅用于日志与后续 UI 提示/重试扩展。 */
  eventsError: string | null;
}

/**
 * 任务 CRUD Hook 返回值接口。
 */
interface UseTaskReturn {
  /** 在指定工作区创建新任务并自动监听首个 turn 的 SSE 流。 */
  createTask: (text: string, workspaceId: string) => Promise<boolean>;
  /** 给当前任务追加一个新轮次并自动监听该轮次 SSE 流。 */
  createTurn: (text: string) => Promise<boolean>;
  /**
   * 加载任务历史事件和轮次，并切换为活跃任务。
   * @param taskId - 待打开的任务标识。
   * @param forceRefresh - 为 true 时忽略内存缓存，强制从后端重新拉取历史事件；
   *   用于在「后端产生了本会话未缓存的新历史」场景下刷新（默认 false 命中缓存跳过拉取）。
   */
  openTask: (taskId: string, forceRefresh?: boolean) => Promise<void>;
  /** 取消当前活跃轮次。 */
  cancelTurn: () => Promise<void>;
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
 * const { createTask, cancelTurn, operation } = useTask();
 *
 * await createTask("帮我写一个登录页面", activeWorkspaceId);
 * if (operation.loading) <Spinner />;
 * ```
 */
export function useTask(): UseTaskReturn {
  const [operation, setOperation] = useState<TaskOperationState>({
    loading: false,
    error: null,
    eventsError: null,
  });

  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  const activeTurnId = useTaskStore((s) => s.activeTurnId);
  const selectedAgentId = useTaskStore((s) => s.selectedAgentId);
  const addTask = useTaskStore((s) => s.addTask);
  const replaceTask = useTaskStore((s) => s.replaceTask);
  const removeTask = useTaskStore((s) => s.removeTask);
  const updateTask = useTaskStore((s) => s.updateTask);
  const setActiveTask = useTaskStore((s) => s.setActiveTask);
  const setActiveTurn = useTaskStore((s) => s.setActiveTurn);
  const setEvents = useEventStore((s) => s.setEvents);
  const setTurnsForTask = useTurnStore((s) => s.setTurnsForTask);
  const upsertTurn = useTurnStore((s) => s.upsertTurn);
  const replaceTurnId = useTurnStore((s) => s.replaceTurnId);
  const removeTurnId = useTurnStore((s) => s.removeTurnId);
  const setStreamingTurn = useTurnStore((s) => s.setStreamingTurn);
  const { connect, disconnect } = useSSE();

  // openTask 竞态防护：单调递增的请求序号。每次 openTask 进入时自增并记录，
  // 异步 await 之后只有「本次仍是最新一次 openTask」才允许 setActiveTask 切活跃。
  // 防止快速连点 taskA→taskB 时，慢的 taskA 过期响应后返回把活跃任务覆盖回 taskA
  // （经典「后发先至」竞态）。事件缓存（setEvents）不受此防护——历史不可变，
  // 过期响应的事件仍按 taskId 落缓存，下次打开即命中，不浪费。
  const openTaskSeqRef = useRef(0);

  /**
   * 在工作区内创建新任务并启动首个 turn 的 SSE 监听。
   *
   * @param text - 用户输入的任务文本。
   * @param workspaceId - 必填的工作区 ID。
   *
   * @sideeffect
   * - POST /workspaces/{workspace_id}/tasks 创建任务与首个 turn
   * - 更新 taskStore.tasksByWorkspaceId（按 workspace 分组）和 activeTaskId
   * - 建立 `/turns/{turn_id}/stream` SSE 连接接收首个 turn 事件流
   */
  const createTask = useCallback(
    async (text: string, workspaceId: string): Promise<boolean> => {
      setOperation({ loading: true, error: null, eventsError: null });
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace();
      }
      const temporaryTaskId = `temp-${Date.now()}`;

      try {
        // 断开旧的 SSE 连接
        disconnect();

        const now = new Date().toISOString();
        addTask({
          task_id: temporaryTaskId,
          workspace_id: workspaceId,
          agent_id: selectedAgentId,
          title: text.slice(0, 80),
          status: "pending",
          execution_status: "pending",
          created_at: now,
          updated_at: now,
        });
        setActiveTask(temporaryTaskId, null);

        // 调用 API 创建任务
        const task = await api.createTask({
          text,
          workspace_id: workspaceId,
          agent_id: selectedAgentId,
        });
        const turns = await api.listTaskTurns(task.task_id);
        const firstTurn = turns[turns.length - 1] ?? null;

        // 同步到 store
        replaceTask(temporaryTaskId, task);
        setTurnsForTask(task.task_id, turns);
        setActiveTask(task.task_id, firstTurn?.turn_id ?? null);
        // 若该 workspace 尚未加载任务列表（未展开过），新任务不会被 replaceTask 写入分组
        // （store 为避免伪造加载态而对未加载分组拒收）。此处显式触发一次加载闭合路径，
        // 确保新建任务在分组中可见；已加载分组则 loadWorkspaceTasks 去重跳过。
        if (!useTaskStore.getState().isWorkspaceLoaded(workspaceId)) {
          void loadWorkspaceTasks(workspaceId);
        }

        // 后端已在创建任务时同步建立首个 pending turn；用真实 turn_id 建立 SSE
        // 连接，驱动该轮次运行（与现有 turn 运行模型一致）。
        if (firstTurn) {
          setStreamingTurn(firstTurn.turn_id);
          await connect(task.task_id, firstTurn.turn_id);
        }

        setOperation({ loading: false, error: null, eventsError: null });
        return true;
      } catch (err) {
        const message = err instanceof Error ? err.message : "创建任务失败";
        logError("createTask 失败", err, { module: "useTask" });
        removeTask(temporaryTaskId);
        setOperation({ loading: false, error: message, eventsError: null });
        return false;
      } finally {
        if (ownsOperation) {
          endClientTrace();
        }
      }
    },
    [addTask, replaceTask, removeTask, setActiveTask, connect, disconnect, setStreamingTurn, setTurnsForTask, selectedAgentId],
  );

  /**
   * 给当前活跃任务追加新轮次并启动 turn 级 SSE。
   *
   * 采用乐观更新：先以临时 turn_id（`temp-${Date.now()}`）把用户输入插入 turnStore
   * 并切换 activeTurnId，使 ChatPanel 立即渲染用户指令，不阻塞在 createTaskTurn 请求上；
   * 后端返回真实 turn 后用 `replaceTurnId` 整体替换临时记录（turn_id 与后续 SSE 对齐、
   * input_text 保持一致，渲染层无感知）；若请求失败则用 `removeTurnId` 回滚临时记录并复位
   * activeTurnId（仅当当前活跃轮仍为该临时 turn 时）。
   *
   * @param text - 本轮用户输入文本。
   *
   * @returns 创建成功（含 SSE 连接建立）返回 true；参数缺失或请求失败返回 false。
   *
   * @sideeffect
   * - 乐观：先 upsertTurn(临时 turn) + setActiveTurn(临时 id)，用户输入立即可见
   * - POST /tasks/{task_id}/turns 创建 pending turn，成功后 replaceTurnId 回写真实 turn
   * - 更新 taskStore.activeTurnId / 任务执行态
   * - 连接 /turns/{turn_id}/stream（SSE 连接错误由 useSSE 内部 markFailed 处理，不会到达本 catch）
   * - 失败回滚：removeTurnId(临时 turn) + 复位 activeTurnId（仅覆盖 createTaskTurn 异常路径）
   */
  const createTurn = useCallback(
    async (text: string): Promise<boolean> => {
      if (!activeTaskId) {
        setOperation({ loading: false, error: "未选择任务", eventsError: null });
        return false;
      }
      setOperation({ loading: true, error: null, eventsError: null });
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace({ taskId: activeTaskId });
      }

      // 乐观更新：先以临时 turn_id 插入用户输入，使 ChatPanel 立即渲染用户指令，
      // 不等后端 createTaskTurn 返回。后端返回真实 turn 后整体替换临时记录。
      const temporaryTurnId = `temp-${Date.now()}`;
      const now = new Date().toISOString();
      const optimisticTurn: TurnRecord = {
        turn_id: temporaryTurnId,
        task_id: activeTaskId,
        input_text: text,
        status: "pending",
        end_reason: null,
        response_text: null,
        created_at: now,
        updated_at: now,
      };
      upsertTurn(optimisticTurn);
      setActiveTurn(temporaryTurnId);

      try {
        disconnect();
        PerfTrace.markCurrent("createTurn:before-post-createTaskTurn", { task_id: activeTaskId });
        const turn = await api.createTaskTurn(activeTaskId, { input_text: text, agent_id: selectedAgentId });
        PerfTrace.markCurrent("createTurn:after-post-createTaskTurn", { turn_id: turn.turn_id, status: turn.status });
        // 后端返回真实 turn：用真实记录整体替换临时记录，turn_id 与后续 SSE 对齐。
        replaceTurnId(activeTaskId, temporaryTurnId, turn);
        setActiveTurn(turn.turn_id);
        updateTask(activeTaskId, {
          execution_status: turn.status,
        });
        setStreamingTurn(turn.turn_id);
        PerfTrace.markCurrent("createTurn:before-connect", { turn_id: turn.turn_id });
        await connect(activeTaskId, turn.turn_id);
        PerfTrace.markCurrent("createTurn:after-connect", { turn_id: turn.turn_id });
        setOperation({ loading: false, error: null, eventsError: null });
        return true;
      } catch (err) {
        // 回滚乐观插入的临时 turn，避免界面残留一条无后端对应的用户消息。
        removeTurnId(activeTaskId, temporaryTurnId);
        if (useTaskStore.getState().activeTurnId === temporaryTurnId) {
          setActiveTurn(null);
        }
        const message = err instanceof Error ? err.message : "创建轮次失败";
        logError("createTurn 失败", err, { module: "useTask", task_id: activeTaskId });
        setOperation({ loading: false, error: message, eventsError: null });
        return false;
      } finally {
        if (ownsOperation) {
          endClientTrace();
        }
      }
    },
    [activeTaskId, connect, disconnect, selectedAgentId, setActiveTurn, setStreamingTurn, updateTask, upsertTurn, replaceTurnId, removeTurnId],
  );

  /**
   * 加载任务历史并切换当前任务。
   *
   * 采用「先渲染后回填」策略以加速历史回放：先并行拉取 task/turns 并立即
   * ``setActiveTask`` 让中央会话区出现（用户消息与 turn 骨架），随后异步拉取
   * 体积更大的历史事件流并 ``setEvents`` 增量投影。这样面板不必等全量事件到达
   * 才出现，显著缩短「点击任务后等待加载」的体感时长。
   *
   * @param taskId - 待打开的任务标识。
   * @param forceRefresh - 为 true 时忽略内存缓存，强制从后端重新拉取历史事件；
   *   用于「后端产生了本会话未缓存的新历史」场景（如其它会话/进程写入了历史）。
   *   默认 false：命中缓存即跳过拉取，复用既有 events 引用避免击穿 TurnTimeline memo。
   *
   * @throws 拉取 task/turns 失败时 rethrow 原始错误（通常为 ``ServiceError``），
   *   供调用方区分 404 与网络错误；历史事件回填失败属非致命，被内部单独捕获不抛出。
   *
   * @sideeffect 从后端读取 task/turns/events 并写入对应 store；历史事件经
   *   eventStore 缓存，跨任务切换不重复拉取（历史对话不可变）。
   */
  const openTask = useCallback(
    async (taskId: string, forceRefresh = false): Promise<void> => {
      const seq = ++openTaskSeqRef.current;
      setOperation({ loading: true, error: null, eventsError: null });
      try {
        // 先拉 task + turns 并立刻渲染骨架：两者体量与事件流相比很小，能快速出首屏。
        const [task, turns] = await Promise.all([
          api.getTask(taskId),
          api.listTaskTurns(taskId),
        ]);
        if (useTaskStore.getState().getTaskById(taskId)) {
          updateTask(taskId, task);
        } else {
          addTask(task);
        }
        setTurnsForTask(taskId, turns);
        // 关键：先切活跃任务触发 ChatPanel 首屏渲染，不等历史事件全量到达。
        // 竞态防护：仅当本次 openTask 仍是最新一次时才切活跃；否则说明用户已切到
        // 更新任务，过期响应的 task/turns 仍可缓存（updateTask/setTurnsForTask 无副作用
        // 于活跃态），但不得覆盖用户当前所在的活跃任务。
        if (seq === openTaskSeqRef.current) {
          setActiveTask(taskId, turns.length > 0 ? turns[turns.length - 1].turn_id : null);
        }
        setOperation({ loading: false, error: null, eventsError: null });

        // 再异步拉历史事件：到达后按 task 分组增量灌入，timeline 自然补全。
        // 即使此期间用户切走，events 仍按 taskId 落缓存，下次打开即命中。
        // 回填失败仅标记 eventsError：骨架已就绪，不回退为"打开失败"态，保留已渲染内容。
        //
        // 内存缓存命中优化（性能关键）：历史对话不可变，且实时 SSE 已通过 appendEvent
        // 并入同一缓存。若本会话内该 task 的事件已缓存（非空）且未要求强制刷新，则跳过
        // 「重新拉取 + setEvents」，直接复用既有 eventsByTurnId 的数组引用——否则每次打开/
        // 切换任务都会让所有 turn 的 events 引用失效，击穿 TurnTimeline 的 memo，造成
        // 「加载历史对话时整棵 timeline 全量重投影 + 重渲染 markdown」卡顿。仅当缓存为空
        // （冷启动首次打开）或 forceRefresh=true（后端有新历史）才走网络回填。
        const cachedEvents = useEventStore.getState().eventsByTaskId[taskId];
        if (!forceRefresh && cachedEvents && cachedEvents.length > 0) {
          setOperation({ loading: false, error: null, eventsError: null });
        } else {
          try {
            const events = await api.listTaskEvents(taskId);
            setEvents(events, taskId);
          } catch (err) {
            const message = err instanceof Error ? err.message : "历史事件加载失败";
            logError("openTask 历史事件回填失败", err, { module: "useTask", task_id: taskId });
            setOperation((prev) => ({ ...prev, eventsError: message }));
          }
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : "打开任务失败";
        logError("openTask 失败", err, { module: "useTask", task_id: taskId });
        setOperation({ loading: false, error: message, eventsError: null });
        // rethrow 原始错误：调用方（如启动恢复 hook）需据此区分 404（清理持久化）
        // 与网络错误（保留持久化重试），不能只吞掉错误。事件回填失败（第 336-340 行）
        // 仍属非致命、被单独捕获，不走到这里。
        throw err;
      }
    },
    [addTask, setActiveTask, setEvents, setTurnsForTask, updateTask],
  );

  /**
   * 取消当前活跃轮次。
   *
   * @sideeffect POST /turns/{id}/cancel + 更新 store 状态。
   */
  const cancelTurn = useCallback(async (): Promise<void> => {
    if (!activeTaskId) return;
    const turnId = activeTurnId ?? useTaskStore.getState().activeTurnId;
    if (!turnId) {
      setOperation({ loading: false, error: "当前任务没有可取消的轮次", eventsError: null });
      return;
    }

    setOperation({ loading: true, error: null, eventsError: null });
    const ownsOperation = !hasClientTrace();
    if (ownsOperation) {
      beginClientTrace({ taskId: activeTaskId });
    }

    try {
      const updated = await api.cancelTurn(turnId, activeTaskId);
      upsertTurn(updated);
      updateTask(activeTaskId, {
        execution_status: updated.status,
        updated_at: updated.updated_at,
      });

      setOperation({ loading: false, error: null, eventsError: null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "取消任务失败";
      logError("cancelTurn 失败", err, { module: "useTask", task_id: activeTaskId, turn_id: turnId });
      setOperation({ loading: false, error: message, eventsError: null });
    } finally {
      if (ownsOperation) {
        endClientTrace();
      }
    }
  }, [activeTaskId, activeTurnId, updateTask, upsertTurn]);

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
      updateTask(activeTaskId, {
        status: task.status,
        execution_status: task.execution_status,
        updated_at: task.updated_at,
      });
    } catch (err) {
      logError("refreshTask 失败", err, { module: "useTask", task_id: activeTaskId });
    } finally {
      if (ownsOperation) {
        endClientTrace();
      }
    }
  }, [activeTaskId, updateTask]);

  return { createTask, createTurn, openTask, cancelTurn, refreshTask, operation };
}
