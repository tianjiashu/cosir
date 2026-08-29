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
import { useContextUsageStore } from "../stores/contextUsageStore";
import { useSSE } from "./useSSE";
import * as api from "../services/api";
import type { TurnRecord } from "@shared/turn";
import type { TaskRecord } from "@shared/task";
import type { AttachmentRef } from "@shared/attachment";
import { logError } from "../lib/logger";
import { beginClientTrace, endClientTrace, hasClientTrace } from "../services/tracePropagation";
import { PerfTrace } from "../lib/perf";

/**
 * 用 ``GET /tasks/{id}`` 返回的持久化占用回填指定任务的 usage 缓存。
 *
 * 单调守卫：本地条目比后端值更新时跳过回填。启动恢复（``useStartupTaskResume``）
 * 会对正在流式的任务回调 ``openTask``，此时后端 ``updated_at`` 是上一轮写入的时间，
 * 无条件回填会把圆环刚积累到的实时值往回拨一格。
 *
 * 后端两个字段任一缺失（尚无 turn / 未落库模型名）时清除该任务缓存，使圆环回落 0/0，
 * 而不是留下一条与新任务状态不符的陈旧占用。
 *
 * @param task - 后端返回的任务记录（含 context_usage_used / context_window_total）。
 *
 * @sideeffect 写入 contextUsageStore 中该任务的条目；不发起网络请求。
 */
function _backfillContextUsage(task: TaskRecord): void {
  const store = useContextUsageStore.getState();
  if (task.context_usage_used == null || task.context_window_total == null) {
    store.resetTask(task.task_id);
    return;
  }
  const current = store.usageByTaskId[task.task_id];
  if (current?.updatedAt != null && current.updatedAt >= task.updated_at) {
    return;
  }
  store.setUsage(
    task.task_id,
    { used_tokens: task.context_usage_used, total_tokens: task.context_window_total },
    task.updated_at,
  );
}

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
  createTask: (text: string, workspaceId: number, attachments?: AttachmentRef[]) => Promise<boolean>;
  /** 给当前任务追加一个新轮次并自动监听该轮次 SSE 流。 */
  createTurn: (text: string, attachments?: AttachmentRef[]) => Promise<boolean>;
  /**
   * 加载任务历史事件和轮次，并切换为活跃任务。
   * @param taskId - 待打开的任务标识（number，后端 int 主键）。
   * @param forceRefresh - 为 true 时忽略内存缓存，强制从后端重新拉取历史事件；
   *   用于在「后端产生了本会话未缓存的新历史」场景下刷新（默认 false 命中缓存跳过拉取）。
   */
  openTask: (taskId: number, forceRefresh?: boolean) => Promise<void>;
  /** 取消当前活跃轮次。 */
  cancelTurn: () => Promise<void>;
  /** 删除任务：级联清后端 + 断开其残留 SSE 流 + 清本地缓存。 */
  deleteTask: (taskId: number) => Promise<void>;
  /** 断开一组任务的残留 SSE 流（删除工作区前调用，不触碰本地缓存）。 */
  disconnectTasks: (taskIds: number[]) => number;
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
  // 选中模型与推理档位均不在此处订阅：createTurn 在请求发出那一刻经 getState()
  // 实时读取，避免回调闭包捕获过期快照——模型过期会提交残缺配对（后端 400），
  // 档位过期会把用户刚改的档位回退成旧值。二者同源处理，本文件无渲染层消费点。
  const addTask = useTaskStore((s) => s.addTask);
  const replaceTask = useTaskStore((s) => s.replaceTask);
  const removeTask = useTaskStore((s) => s.removeTask);
  const updateTask = useTaskStore((s) => s.updateTask);
  const setActiveTask = useTaskStore((s) => s.setActiveTask);
  const setActiveTurn = useTaskStore((s) => s.setActiveTurn);
  const setInputDraft = useTaskStore((s) => s.setInputDraft);
  const setEvents = useEventStore((s) => s.setEvents);
  const setTurnsForTask = useTurnStore((s) => s.setTurnsForTask);
  const upsertTurn = useTurnStore((s) => s.upsertTurn);
  const replaceTurnId = useTurnStore((s) => s.replaceTurnId);
  const removeTurnId = useTurnStore((s) => s.removeTurnId);
  const setStreamingTurn = useTurnStore((s) => s.setStreamingTurn);
  const { connect, disconnectTurn, disconnectTask, disconnectTasks } = useSSE();

  // openTask 竞态防护：单调递增的请求序号。每次 openTask 进入时自增并记录，
  // 异步 await 之后只有「本次仍是最新一次 openTask」才允许 setActiveTask 切活跃。
  // 防止快速连点 taskA→taskB 时，慢的 taskA 过期响应后返回把活跃任务覆盖回 taskA
  // （经典「后发先至」竞态）。事件缓存（setEvents）不受此防护——历史不可变，
  // 过期响应的事件仍按 taskId 落缓存，下次打开即命中，不浪费。
  const openTaskSeqRef = useRef(0);
  // 乐观临时 turn 的负 number 占位自增序号：每个临时 turn 用 turn_id = -1 - (++seq)
  // 作为唯一 number 占位（与 TurnRecord.turn_id 的 number 类型一致，不污染 store 的 number
  // 主通道）。临时 id 的 string 维度（"temp-<uuid>"）仅用于 SSE 连接键（useSSE.connectionsRef
  // 为 Map<string>），不写入 store。
  const pendingTurnSeqRef = useRef(0);
  // 乐观临时 task 的负 number 占位自增序号：范式同 pendingTurnSeqRef（task_id = -1 - (++seq)）。
  const pendingTaskSeqRef = useRef(0);

  /**
   * 给当前活跃任务追加新轮次并启动 turn 级 SSE。
   *
   * 采用乐观更新：先以负 number 占位 turn_id（`-1 - seq`）把用户输入插入 turnStore
   * 并切换 activeTurnId，使 ChatPanel 立即渲染用户指令，不阻塞在 createTaskTurn 请求上；
   * 后端返回真实 turn 后用 `replaceTurnId` 整体替换临时记录（turn_id 与后续 SSE 对齐、
   * input_text 保持一致，渲染层无感知）；若请求失败则用 `removeTurnId` 回滚临时记录并复位
   * activeTurnId（仅当当前活跃轮仍为该占位 turn 时）。临时 id 的 string 维度（"temp-<uuid>"）
   * 仅用于 SSE 连接键，不写入 store 的 number 字段。
   *
   * @param text - 本轮用户输入文本。
   *
   * @returns 创建成功（含 SSE 连接建立）返回 true；参数缺失或请求失败返回 false。
   *
   * @sideeffect
   * - 乐观：先 upsertTurn(临时 turn，负 number 占位) + setActiveTurn(占位 id)，用户输入立即可见
   * - POST /tasks/{task_id}/turns 创建 pending turn（透传 provider_id + model_name 二元组与
   *   reasoning_effort；reasoning_effort 为 null 时不传，同后端 None=max/不指定），
   *   成功后 replaceTurnId 回写真实 turn
   * - 更新 taskStore.activeTurnId / 任务执行态
   * - 连接 /turns/{turn_id}/stream（SSE 连接错误由 useSSE 内部 markFailed 处理，不会到达本 catch）
   * - 失败回滚：removeTurnId(占位 turn) + 复位 activeTurnId（仅覆盖 createTaskTurn 异常路径）
   */
  const createTurn = useCallback(
    async (text: string, attachments?: AttachmentRef[]): Promise<boolean> => {
      if (!activeTaskId) {
        setOperation({ loading: false, error: "未选择任务", eventsError: null });
        return false;
      }
      setOperation({ loading: true, error: null, eventsError: null });
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace({ taskId: String(activeTaskId) });
      }

      // 乐观更新：先以负 number 占位 turn_id 插入用户输入，使 ChatPanel 立即渲染用户指令，
      // 不等后端 createTaskTurn 返回。后端返回真实 turn 后整体替换临时记录。
      // temporaryTurnId 仅作为 SSE 连接键（string 维度），turn_id 字段用 number 占位。
      const temporaryTurnId = `temp-${crypto.randomUUID()}`;
      const optimisticTurnId = -1 - (++pendingTurnSeqRef.current);
      const now = new Date().toISOString();
      const optimisticTurn: TurnRecord = {
        turn_id: optimisticTurnId,
        task_id: activeTaskId,
        input_text: text,
        status: "pending",
        end_reason: null,
        response_text: null,
        created_at: now,
        updated_at: now,
      };
      upsertTurn(optimisticTurn);
      setActiveTurn(optimisticTurnId);

      try {
        // 断开旧的临时 turn 的 SSE 连接（仅断该 turn，不影响后台其它 task 的并发连接）。
        // 若临时 turn 尚未建立连接，disconnectTurn 为 no-op，安全。
        disconnectTurn(temporaryTurnId);
        PerfTrace.markCurrent("createTurn:before-post-createTaskTurn", { task_id: activeTaskId });
        // 前端不再传递 agent_id，由后端创建轮次时固定 main_agent。
        // provider_id 与 model_name 必须成对提交：后端 turn_service.create_turn 对
        // 「有 model_name 无 provider_id」直接抛 ValueError（API 层映射 400）。
        // 模型与档位都在「请求发出那一刻」从 store 实时读取，而非依赖闭包捕获的快照：
        // 本回调生命周期可能跨越多次模型/档位切换，闭包快照存在静默过期风险——
        // 模型过期会提交残缺配对并复现后端 400，档位过期会把用户已改的新档位
        // 回退成旧值。两者同源处理，一并从 store 现取。
        const storeSnapshot = useTaskStore.getState();
        const currentModel = storeSnapshot.selectedModel;
        const currentEffort = storeSnapshot.selectedReasoningEffort;
        const turn = await api.createTaskTurn(String(activeTaskId), {
          input_text: text,
          provider_id: currentModel?.provider_id,
          model_name: currentModel?.model_name,
          // 推理强度档位透传后端：null（未指定）经 ?? undefined 转为「不传」，
          // 与后端 CreateTurnRequest.reasoning_effort: str | None 语义（None=max/不指定）一致。
          reasoning_effort: currentEffort ?? undefined,
          attachments: attachments && attachments.length > 0 ? attachments : undefined,
        });
        PerfTrace.markCurrent("createTurn:after-post-createTaskTurn", { turn_id: turn.turn_id, status: turn.status });
        // 后端返回真实 turn：用真实记录整体替换临时记录，turn_id 与后续 SSE 对齐。
        replaceTurnId(activeTaskId, optimisticTurnId, turn);
        setActiveTurn(turn.turn_id);
        updateTask(activeTaskId, {
          execution_status: turn.status,
        });
        // 按 task 维度写入 streaming turn，支持多 task 并发流式时各自独立停止。
        setStreamingTurn(activeTaskId, turn.turn_id);
        PerfTrace.markCurrent("createTurn:before-connect", { turn_id: turn.turn_id });
        await connect(activeTaskId, turn.turn_id);
        PerfTrace.markCurrent("createTurn:after-connect", { turn_id: turn.turn_id });
        setOperation({ loading: false, error: null, eventsError: null });
        return true;
      } catch (err) {
        // 回滚乐观插入的临时 turn，避免界面残留一条无后端对应的用户消息。
        removeTurnId(activeTaskId, optimisticTurnId);
        if (useTaskStore.getState().activeTurnId === optimisticTurnId) {
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
    [
      activeTaskId,
      connect,
      disconnectTurn,
      setActiveTurn,
      setStreamingTurn,
      updateTask,
      upsertTurn,
      replaceTurnId,
      removeTurnId,
    ],
  );

  /**
   * 在工作区内创建新任务并显式创建首个 turn、启动首个 turn 的 SSE 监听。
   *
   * 任务创建与首轮次创建已解耦（对齐后端 `create_workspace_task` 仅建 task 容器）：
   * 先 POST `/workspaces/{workspace_id}/tasks` 创建 task 容器，再显式调 `createTurn`
   * （复用本 hook 的 createTurn 方法）创建首个 turn 并接入 SSE。前端无 Agent 选择 UI，
   * agent 维度由后端 turns_api 硬编码 main_agent 承载，task 维度不再含 agent_id。
   *
   * @param text - 用户输入的任务文本。
   * @param workspaceId - 必填的工作区 ID（后端 int 主键）。
   *
   * @sideeffect
   * - POST /workspaces/{workspace_id}/tasks 创建任务容器（仅 text/workspace_id）
   * - 显式 createTurn 创建首个 turn（provider_id + model_name 二元组 / attachments 经
   *   turn 请求体传递）
   * - 更新 taskStore.tasksByWorkspaceId（按 workspace 分组）和 activeTaskId
   * - 建立 `/turns/{turn_id}/stream` SSE 连接接收首个 turn 事件流
   */
  const createTask = useCallback(
    async (text: string, workspaceId: number, attachments?: AttachmentRef[]): Promise<boolean> => {
      setOperation({ loading: true, error: null, eventsError: null });
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace();
      }
      const temporaryTaskId = `temp-${crypto.randomUUID()}`;
      // 乐观 task 的 number 占位：与临时 turn 同范式（task_id = -1 - seq），
      // 仅用于 store 的 number 主通道定位；temporaryTaskId 的 string 维度仅作 SSE 连接键。
      const optimisticTaskId = -1 - (++pendingTaskSeqRef.current);

      try {
        // 断开旧的临时 task 的 SSE 连接（仅断该 turn，不影响后台其它 task 的并发连接）。
        // 若临时 task 尚未建立连接，disconnectTurn 为 no-op，安全。
        disconnectTurn(temporaryTaskId);

        const now = new Date().toISOString();
        addTask({
          task_id: optimisticTaskId,
          workspace_id: workspaceId,
          title: text.slice(0, 80),
          status: "pending",
          execution_status: "pending",
          created_at: now,
          updated_at: now,
        });
        setActiveTask(optimisticTaskId, null);
        // 草稿暂存到乐观占位 ID：此时尚无真实 task_id，先把未发送内容挂到占位键，
        // replaceTask 转正时会迁移到真实 ID（迁移链路见 taskStore.replaceTask）。
        // 这样即便首 turn 失败，草稿也不会因「无 task_id 可写」而丢失。
        setInputDraft(optimisticTaskId, text);

        // 创建 task 容器（请求体仅 text/workspace_id，后端不再要求 agent_id）。
        const task = await api.createTask({
          text,
          workspace_id: workspaceId,
        });
        replaceTask(optimisticTaskId, task);
        // 若该 workspace 尚未加载任务列表（未展开过），新任务不会被 replaceTask 写入分组
        // （store 为避免伪造加载态而对未加载分组拒收）。此处显式触发一次加载闭合路径，
        // 确保新建任务在分组中可见；已加载分组则 loadWorkspaceTasks 去重跳过。
        if (!useTaskStore.getState().isWorkspaceLoaded(workspaceId)) {
          void loadWorkspaceTasks(workspaceId);
        }

        // 显式创建首个 turn（复用本 hook 的 createTurn），其内会建 SSE 并写 activeTurnId。
        // 此时 activeTaskId 已切换为真实 task，createTurn 能正确归属。
        const turnOk = await createTurn(text, attachments);
        if (!turnOk) {
          // 首 turn 失败：回滚 task 记录，避免留下无 turn 的空任务残留。
          // 草稿链路闭环：草稿先前挂在乐观占位 ID 上，经 replaceTask 迁移到真实
          // task.task_id；此处 removeTask 清除的正是这份真实 ID 草稿（store 内已
          // 保证内存态与落盘同步清除）。用户未发送内容不依赖它保活——useSendInput
          // 的 local.restore 会把原文回填本地输入框，用户视线内不丢草稿。
          removeTask(task.task_id);
          // 保留 createTurn 已写入的具体错误（如后端 400 明细），不覆盖为笼统文案。
          setOperation((prev) => ({ ...prev, loading: false }));
          return false;
        }

        setOperation({ loading: false, error: null, eventsError: null });
        return true;
      } catch (err) {
        const message = err instanceof Error ? err.message : "创建任务失败";
        logError("createTask 失败", err, { module: "useTask" });
        removeTask(optimisticTaskId);
        setOperation({ loading: false, error: message, eventsError: null });
        return false;
      } finally {
        if (ownsOperation) {
          endClientTrace();
        }
      }
    },
    [
      addTask,
      replaceTask,
      removeTask,
      setActiveTask,
      setInputDraft,
      connect,
      disconnectTurn,
      createTurn,
    ],
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
    async (taskId: number, forceRefresh = false): Promise<void> => {
      const seq = ++openTaskSeqRef.current;
      setOperation({ loading: true, error: null, eventsError: null });
      try {
        // 先拉 task + turns 并立刻渲染骨架：两者体量与事件流相比很小，能快速出首屏。
        const [task, turns] = await Promise.all([
          api.getTask(String(taskId)),
          api.listTaskTurns(String(taskId)),
        ]);
        if (useTaskStore.getState().getTaskById(taskId)) {
          updateTask(taskId, task);
        } else {
          addTask(task);
        }
        setTurnsForTask(taskId, turns);

        // 回填上下文窗口占用：任务级真实占用由后端持久（context_usage_used），
        // 窗口上限由后端按模型动态计算（context_window_total）。打开已有任务时
        // 立刻呈现，无需等待下一次 CONTEXT_USAGE 事件；后端无占用数据时清除该任务缓存。
        //
        // 按 taskId 写入（而非全局覆盖）：即便本次响应已过期（用户在 await 期间切走），
        // 最多刷新该任务自己的缓存条目，圆环由 activeTaskId 选择展示，过期响应在结构
        // 上不可能污染当前 UI。
        _backfillContextUsage(task);
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
            const events = await api.listTaskEvents(String(taskId));
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
      beginClientTrace({ taskId: String(activeTaskId) });
    }

    try {
      const updated = await api.cancelTurn(String(turnId), String(activeTaskId));
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
  /**
   * 删除任务并清理其全部本地与运行期残留。
   *
   * 删除事务必须整体收口在本 hook 内，而不是让调用方各自拼装：SSE 连接池由
   * ``useSSE`` 持有（本 hook 是生产环境唯一实例化方），调用方拿不到 ``disconnectTask``。
   * 少了断流这一步，正在流式的任务被删除后其残留流会继续投递事件，在 eventStore /
   * turnStore / contextUsageStore 中重建已删任务的条目。
   *
   * 顺序：先断流（阻止新事件进入攒批缓冲）→ 再调后端删除 → 最后清本地缓存。
   * 断流前置可避免删除期间到达的事件在 store 清理之后又写回条目。
   *
   * @param taskId - 待删除的任务标识（number，后端 int 主键）。
   *
   * @throws 后端删除失败时抛出原始错误，由调用方决定是否展示；此时本地缓存保持
   *   不变（任务未被删除），但流已断开需用户重新打开任务才能恢复。
   *
   * @sideeffect DELETE /tasks/{id}；断开该任务的 SSE 流；清除该任务的事件缓存、
   *   上下文占用缓存与输入草稿（后两者由 taskStore.removeTask 内部收口）。
   */
  const deleteTask = useCallback(
    async (taskId: number): Promise<void> => {
      disconnectTask(taskId);
      await api.deleteTask(String(taskId));
      removeTask(taskId);
    },
    [disconnectTask, removeTask],
  );

  const refreshTask = useCallback(async (): Promise<void> => {
    if (!activeTaskId) return;
    const ownsOperation = !hasClientTrace();
    if (ownsOperation) {
      beginClientTrace({ taskId: String(activeTaskId) });
    }

    try {
      const task = await api.getTask(String(activeTaskId));
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

  return {
    createTask,
    createTurn,
    openTask,
    cancelTurn,
    deleteTask,
    disconnectTasks,
    refreshTask,
    operation,
  };
}
