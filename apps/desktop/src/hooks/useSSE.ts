/**
 * SSE 事件流连接 Hook。
 *
 * 封装 SSE 连接的完整生命周期：
 * - 连接 / 断开 / 重连
 * - 事件分发到 eventStore（含回放去重）
 * - 错误处理与状态管理
 *
 * 桌面端支持「task 间并发、同 task turn 串行」语义：本 Hook 不再持有全局唯一连接，
 * 而是操作按 turnId 维度隔离的连接池 ``sseConnectionPool``。每个 turn 拥有独立连接，
 * 断开某一 turn 的连接不会影响其它 turn 的实时流，从而支撑后台 task 正在流式时前台
 * task 发消息互不断开。
 *
 * 连接池是模块级单例（不放在 useRef 里）：``useTask()`` 在多个组件中各自实例化，
 * 若池随 hook 实例分裂，Sidebar 的断流将看不到 InputBar 建立的连接而静默失效。
 * 本 Hook 只负责 React 侧的绑定——攒批调度、状态回写与卸载清理。
 *
 * 组件只需调用 connect(taskId, turnId) 即可开始接收该轮次的事件流；
 * 断开某个 turn 用 disconnectTurn(turnId)，断开某任务全部流用 disconnectTask(taskId)，
 * 全局清理用 disconnectAll()。
 *
 * @module hooks/useSSE
 */

import { useCallback, useRef } from "react";
import type { ContextUsagePayload, RuntimeEvent } from "@shared/events";
import type { TaskStatus } from "@shared/task";
import type { TurnStatus } from "@shared/turn";
import { useEventStore } from "../stores/eventStore";
import { useTaskStore } from "../stores/taskStore";
import { useTurnStore } from "../stores/turnStore";
import { useContextUsageStore } from "../stores/contextUsageStore";
import { SSEConnection, SSEConnectionState, type SSEErrorHandler } from "../services/sse";
import { sseConnectionPool } from "../services/sseConnectionPool";
import { logError, logDebug } from "../lib/logger";
import { PerfTrace } from "../lib/perf";

/**
 * SSE 事件流 Hook 返回值接口。
 */
interface UseSSEReturn {
  /**
   * 建立 SSE 连接并开始后台接收指定任务的事件流。
   *
   * @param taskId - 要监听的任务 ID（number，后端 int 主键）；内部经 `String()` 转为
   *   SSE URL 层的 string 键（SSEConnection / connectionsRef 的键维度为 string，属 URL 层）。
   * @param turnId - 要监听的轮次 ID（number，后端 int 主键）；同上经 `String()` 转 URL 键。
   * @returns Promise<void>，连接调度后 resolve；后续流错误通过日志和状态回调处理。
   */
  connect: (taskId: number, turnId: number) => Promise<void>;

  /** 断开指定 turn 的 SSE 连接（不影响其它 turn）。 */
  disconnectTurn: (turnId: string) => void;

  /**
   * 断开指定 task 下全部 turn 的 SSE 连接（不影响其它 task）。
   * 删除任务时调用：任务已被后端级联删除，其残留流必须一并断开，否则事件会继续
   * 投递并在各 store 中重建已删任务的条目。
   */
  disconnectTask: (taskId: number) => void;

  /**
   * 断开指定多个 task 下全部 turn 的 SSE 连接（不影响其它 task）。
   * 删除工作区时调用，作用域为被级联删除的那组任务。
   */
  disconnectTasks: (taskIds: number[]) => number;

  /** 当前连接状态（从 eventStore 派生）。 */
  connectionState: SSEConnectionState;
}

/**
 * SSE 事件流连接 Hook。
 *
 * 管理单个 SSE 连接实例的生命周期，
 * 自动将接收的事件分发到 eventStore.appendEvent()，
 * 并通过 store 更新连接状态。
 *
 * @example
 * ```tsx
 * const { connect, disconnectTurn, disconnectTask, disconnectAll, connectionState } = useSSE();
 *
 * // 开始监听指定轮次事件
 * await connect(taskId, turnId);
 *
 * // 仅断开该轮次（不影响其它并发轮次）
 * disconnectTurn("turn-uuid");
 *
 * // 删除任务时断开其全部残留流
 * disconnectTask(taskId);
 *
 * // 删除工作区时断开被级联删除的那组任务的残留流
 * disconnectTasks([taskIdA, taskIdB]);
 * ```
 */
export function useSSE(): UseSSEReturn {
  const appendEvents = useEventStore((s) => s.appendEvents);
  const setConnectionState = useEventStore((s) => s.setConnectionState);
  const connectionState = useEventStore((s) => s.connectionState);
  const updateTask = useTaskStore((s) => s.updateTask);
  const updateTurn = useTurnStore((s) => s.updateTurn);
  const setStreamingTurn = useTurnStore((s) => s.setStreamingTurn);
  const setTaskUsage = useContextUsageStore((s) => s.setUsage);

  // 攒批缓冲：把同一动画帧内的多个 delta 合并成一次 appendEvents + 一次渲染，
  // 将高频流式下的 set/投影/重渲染压力从「每 delta 一次」降到「每帧一次」，
  // 对高 token 率与长会话（后续迭代常见场景）提供稳定的渲染节奏兜底。
  const pendingEventsRef = useRef<RuntimeEvent[]>([]);
  const firstEventSeenRef = useRef<boolean>(false);
  const rafRef = useRef<number | null>(null);
  const flushRef = useRef<() => void>(() => {});

  /**
   * 建立到指定任务轮次的 SSE 连接。
   *
   * @param taskId - 要监听的任务标识（number，后端 int 主键）；内部 `String()` 转为
   *   SSE URL 层与 connectionsRef 的 string 键（URL/连接池维度，合理为 string）。
   * @param turnId - 要监听的轮次标识（number，后端 int 主键）；同上经 `String()` 转 URL 键。
   * @returns Promise<void>，连接调度后 resolve；流式消费在后台继续。
   */
  const connect = useCallback(
    async (taskId: number, turnId: number): Promise<void> => {
      // SSE URL 层与连接池键维度为 string（"temp-<uuid>" 或 String(turnId)），
      // 此处把 number 主键转为 string 键，与 store 的 number 主通道解耦。
      const connKey = String(turnId);
      PerfTrace.markCurrent("sse:connect-start", { task_id: taskId, turn_id: turnId });
      // 先 flush 上一连接已入缓冲、尚未到下一动画帧的事件，避免快速重连时静默丢弃
      // （连接池存的是 SSEConnection，其 disconnect 只 abort 不 flush；断流时的 flush
      // 由连接池按条目回调，故这里必须显式 flush 以冲刷本实例缓冲中旧连接的残留事件）。
      flushRef.current();
      // 旧连接已断开、不再产生事件；重置共享缓冲，避免新旧连接复用同一数组
      // 造成的事件归属耦合或快速重连场景下的缓冲污染。
      pendingEventsRef.current = [];
      firstEventSeenRef.current = false;

      const markFailed = (error: Error) => {
        const now = new Date().toISOString();
        updateTask(taskId, { execution_status: "failed", updated_at: now });
        updateTurn(taskId, turnId, {
          status: "failed",
          end_reason: error.message,
          updated_at: now,
        });
        setStreamingTurn(taskId, null);
      };

      // 延迟应用到终态的错误。onError 在流读完时被 SSEConnection **同步**触发，
      // 而此时 run_started 等事件仍滞留在 pendingEventsRef 等待 rAF 攒批 flush；
      // 若在此立即 markFailed，随后 connect().finally() 的兜底 flush 会用
      // syncRuntimeStatus(run_started) 把 turn 覆盖回 running —— 终态被迟到 flush 覆盖，
      // 思考块永不折叠（isTurnActive 恒为 true）。故先记录错误，待残留事件 flush 完
      // 再应用 markFailed，保证「终态写序在最后」。正常收尾（_terminalReceived）或
      // 主动取消（_aborted）时 onError 不会被触发，此变量恒为 null，不影响正常路径。
      let pendingError: Error | null = null;
      const onError: SSEErrorHandler = (error: Error) => {
        logError("SSE 连接错误", error, { module: "useSSE" });
        pendingError = error;
      };

      // 把缓冲事件一次性提交：单次 appendEvents（单次 set、单次渲染）+ 逐事件同步运行态。
      const flush = () => {
        if (rafRef.current != null) {
          if (typeof cancelAnimationFrame === "function") {
            cancelAnimationFrame(rafRef.current);
          } else {
            clearTimeout(rafRef.current);
          }
          rafRef.current = null;
        }
        const batch = pendingEventsRef.current;
        if (batch.length === 0) {
          return;
        }
        pendingEventsRef.current = [];
        // 诊断日志：每个 flush 批次里收到的模型流式事件数量与累计增量长度，
        // 用于排查「前端是否收到 delta / delta 是否整块到达」的流式问题。
        const deltaEvents = batch.filter(
          (e) => e.event_type === "model_output_delta" || e.event_type === "model_thinking_delta",
        );
        if (deltaEvents.length > 0) {
          const totalLen = deltaEvents.reduce((sum, e) => {
            const text = (e.payload as { text?: string } | undefined)?.text ?? "";
            return sum + text.length;
          }, 0);
          logDebug("sse_model_deltas_received", {
            module: "useSSE",
            batchSize: batch.length,
            deltaCount: deltaEvents.length,
            outputCount: deltaEvents.filter((e) => e.event_type === "model_output_delta").length,
            thinkingCount: deltaEvents.filter((e) => e.event_type === "model_thinking_delta").length,
            totalDeltaChars: totalLen,
          });
        }
        appendEvents(batch);
        for (const event of batch) {
          syncRuntimeStatus(event, updateTask, updateTurn, setStreamingTurn);
          // 上下文占用为「最新值覆盖」语义，随每帧 flush 的批量事件同步一次，
          // 不进入事件历史流，避免 InputBar 因订阅全量事件而高频重渲染。
          //
          // 按 event.task_id 归属，而非「当前活跃任务」：多个 task 的 turn 会并发
          // 流式，同一个 flush 批次内可能交错不同 task 的事件。分键写入后每个 task
          // 各自保留本批次的最后一条，后台 task 不会覆盖用户正在查看的圆环；圆环
          // 由 ContextUsageRing 按 activeTaskId 选择展示。
          if (event.event_type === "context_usage") {
            setTaskUsage(event.task_id, event.payload as ContextUsagePayload, event.created_at);
          }
        }
      };
      flushRef.current = flush;

      // 安排下一帧 flush；若环境无 rAF 则退化为 setTimeout。
      const scheduleFlush = () => {
        if (rafRef.current != null) {
          return;
        }
        const run = () => {
          rafRef.current = null;
          flushRef.current();
        };
        if (typeof requestAnimationFrame === "function") {
          rafRef.current = requestAnimationFrame(run);
        } else {
          rafRef.current = setTimeout(run, 16) as unknown as number;
        }
      };

      const onEvent = (event: RuntimeEvent) => {
        if (!firstEventSeenRef.current) {
          firstEventSeenRef.current = true;
          PerfTrace.markCurrent("sse:first-event-received", { event_type: event.event_type, turn_id: event.turn_id });
        }
        pendingEventsRef.current.push(event);
        scheduleFlush();
      };

      // 建连不清零任何 task 的占用：占用按 taskId 分键存储，新任务本就没有条目
      // （缺省即 0/0），而清理会误伤后台并发任务的已缓存值。

      const connection = new SSEConnection({
        taskId: String(taskId),
        turnId: String(turnId),
        onEvent,
        onError,
        onStateChange: (state) => {
          if (state === SSEConnectionState.STREAMING) {
            PerfTrace.markCurrent("sse:connection-open", { task_id: taskId, turn_id: turnId });
          }
          // 仅当前活动连接可回写连接状态，避免被已断开的旧连接（竞态）误钉为 CLOSED
          if (sseConnectionPool.get(connKey)?.connection === connection) {
            setConnectionState(state);
          }
        },
      });

      // 放入共享池：同键已有旧连接时先断开它（仅影响该 turn，不动其它 turn）。
      // 同时登记本连接的 flush，供断开方（可能不是本 hook 实例）精确冲刷本连接的
      // 缓冲，而非冲刷调用方自己的缓冲。
      sseConnectionPool.replace(connKey, { connection, taskId, flush });
      void connection.connect()
        .finally(() => {
          // 仅当前活动连接可接管清理与终态回写：极边缘场景下旧连接自然异常结束、同时
          // 用户已重连新连接时，旧连接 finally 不得 flush 共享缓冲（会误提新连接事件）
          // 也不得把新进行中的任务误标 failed。
          const isActive = sseConnectionPool.get(connKey)?.connection === connection;
          if (isActive) {
            sseConnectionPool.remove(connKey);
            // 流结束后兜底 flush 残留事件，避免最后若干 delta 不落盘 / 状态不更新
            // （此时 run_started 可能被映射回 running）。调用本次闭包捕获的 flush，
            // 而非跨连接共享的 flushRef.current()，避免借用新连接的缓冲语义。
            flush();
            // 再应用延迟的错误终态：保证 markFailed 写在残留事件 flush 之后，
            // 终态不被迟到的 run_started 覆盖。正常完成时 pendingError 为 null，无副作用。
            if (pendingError) {
              markFailed(pendingError);
              pendingError = null;
            }
          }
        })
        .catch(() => {
          // SSEConnection 已通过 onError、状态回写和内部日志记录错误，这里只负责避免未处理 Promise。
        });
    },
    [appendEvents, setConnectionState, setStreamingTurn, updateTask, updateTurn, setTaskUsage],
  );

  /**
   * 断开指定 turn 的 SSE 连接（不影响其它 turn 的并发连接）。
   *
   * @param turnId - 要断开的轮次标识；若该 turn 无活动连接则为 no-op。
   */
  // 断开时的 flush 由连接池按条目回调（见 PooledConnection.flush），此处不再调用
  // flushRef：本 hook 在多个组件中各自实例化，断开方往往不是建立方，调用自己的
  // flushRef 会冲刷到无关的缓冲，反而漏掉目标连接的残留事件。
  const disconnectTurn = useCallback((turnId: string) => {
    sseConnectionPool.remove(turnId);
  }, []);

  /**
   * 断开指定任务下全部 turn 的 SSE 连接（不影响其它任务）。
   *
   * 删除任务时调用：后端已级联删除该任务的轮次与事件，其残留流若不断开，会继续
   * 投递事件并在 eventStore / turnStore / contextUsageStore 中重建已删任务的条目。
   * 该任务无活动连接时为空操作。
   *
   * @param taskId - 待断开连接的任务标识（后端 int 主键）。
   */
  const disconnectTask = useCallback((taskId: number) => {
    sseConnectionPool.removeByTask(taskId);
  }, []);

  /**
   * 断开指定多个任务下全部 turn 的 SSE 连接（不影响其它任务）。
   *
   * 删除工作区时调用：后端级联删除该工作区下的全部任务，其残留流若不断开，会继续
   * 投递事件并在各 store 中重建已删任务的条目。这些任务无活动连接时为空操作。
   *
   * @param taskIds - 待断开连接的任务标识列表（后端 int 主键）。
   * @returns 本次实际断开的轮次键数量。
   */
  const disconnectTasks = useCallback((taskIds: number[]) => {
    return sseConnectionPool.removeByTasks(taskIds).length;
  }, []);

  return { connect, disconnectTurn, disconnectTask, disconnectTasks, connectionState };
}

type UpdateTask = ReturnType<typeof useTaskStore.getState>["updateTask"];
type UpdateTurn = ReturnType<typeof useTurnStore.getState>["updateTurn"];
type SetStreamingTurn = ReturnType<typeof useTurnStore.getState>["setStreamingTurn"];

/**
 * 将后端运行事件同步为任务与轮次的展示状态。
 *
 * @param event - 后端 SSE 运行事件。
 * @param updateTask - taskStore 局部更新动作。
 * @param updateTurn - turnStore 局部更新动作。
 * @param setStreamingTurn - streaming turn 更新动作。
 *
 * @sideeffect 写入 taskStore/turnStore；不发起网络请求。
 */
function syncRuntimeStatus(
  event: RuntimeEvent,
  updateTask: UpdateTask,
  updateTurn: UpdateTurn,
  setStreamingTurn: SetStreamingTurn,
): void {
  const status = runtimeStatusFromEvent(event);
  if (!status) return;

  updateTask(event.task_id, {
    execution_status: status.taskStatus,
    updated_at: event.created_at,
  });

  if (event.turn_id) {
    updateTurn(event.task_id, event.turn_id, {
      status: status.turnStatus,
      end_reason: status.endReason,
      updated_at: event.created_at,
    });
    if (status.terminal) {
      setStreamingTurn(event.task_id, null);
    }
  }
}

/**
 * 从 runtime event 推导 task/turn 状态。
 *
 * 终态事件识别范围：run_finished / run_failed / run_cancelled。
 * 注意：「客户端连接断开」不是一个独立的 event_type（RuntimeEventType 中不存在
 * client_disconnected），而是 run_failed 事件的 payload 语义：后端经 finally 兜底标记
 * turn failed 时，会在 run_failed 的 payload 中下发 error/end_reason = "client_disconnected"。
 * 因此对 run_failed 的语义差异处理：
 *   - run_failed 优先采用 payload.end_reason 作为 endReason（如
 *     "client_disconnected"），其次回退到 payload.error 文案，最后回退枚举值
 *     "run_failed"，使上层 UI 能区分「客户端连接断开」与「Agent 真实执行失败」。
 * 终态本身由 run_failed 承载，不存在独立的 client_disconnected 裸事件分支。
 *
 * @param event - 后端 SSE 运行事件。
 * @returns 可同步状态（含终态标记与 endReason）；非运行态事件返回 null。
 */
export function runtimeStatusFromEvent(
  event: RuntimeEvent,
): { taskStatus: TaskStatus; turnStatus: TurnStatus; terminal: boolean; endReason: string | null } | null {
  if (event.event_type === "run_started") {
    return { taskStatus: "active", turnStatus: "running", terminal: false, endReason: null };
  }
  if (event.event_type === "run_finished" || event.event_type === "final_response") {
    return { taskStatus: "completed", turnStatus: "completed", terminal: true, endReason: null };
  }
  if (event.event_type === "run_failed") {
    // 优先保留后端下发的 end_reason（如 client_disconnected），否则用 error 文案兜底，
    // 最后回退到枚举值 "run_failed"。这样 UI 能区分「客户端断开」与「真实执行失败」。
    const payloadEndReason =
      typeof event.payload.end_reason === "string" ? event.payload.end_reason : null;
    const endReason =
      payloadEndReason ?? (typeof event.payload.error === "string" ? event.payload.error : "run_failed");
    return { taskStatus: "failed", turnStatus: "failed", terminal: true, endReason };
  }
  if (event.event_type === "run_cancelled") {
    return { taskStatus: "cancelled", turnStatus: "cancelled", terminal: true, endReason: "run_cancelled" };
  }
  return null;
}
