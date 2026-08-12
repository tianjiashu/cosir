/**
 * SSE 事件流连接 Hook。
 *
 * 封装 SSE 连接的完整生命周期：
 * - 连接 / 断开 / 重连
 * - 事件分发到 eventStore（含回放去重）
 * - 错误处理与状态管理
 *
 * 组件只需调用 connect(taskId, turnId) 即可开始接收事件流。
 *
 * @module hooks/useSSE
 */

import { useCallback, useRef } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TaskStatus } from "@shared/task";
import type { TurnStatus } from "@shared/turn";
import { useEventStore } from "../stores/eventStore";
import { useTaskStore } from "../stores/taskStore";
import { useTurnStore } from "../stores/turnStore";
import { SSEConnection, SSEConnectionState, type SSEErrorHandler } from "../services/sse";
import { logError } from "../lib/logger";
import { PerfTrace } from "../lib/perf";

/**
 * SSE 事件流 Hook 返回值接口。
 */
interface UseSSEReturn {
  /**
   * 建立 SSE 连接并开始后台接收指定任务的事件流。
   *
   * @param taskId - 要监听的任务 ID。
   * @param turnId - 要监听的轮次 ID。
   * @returns Promise<void>，连接调度后 resolve；后续流错误通过日志和状态回调处理。
   */
  connect: (taskId: string, turnId: string) => Promise<void>;

  /** 断开当前 SSE 连接。 */
  disconnect: () => void;

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
 * const { connect, disconnect, connectionState } = useSSE();
 *
 * // 开始监听任务事件
 * await connect("task-uuid");
 *
 * // 停止监听
 * disconnect();
 * ```
 */
export function useSSE(): UseSSEReturn {
  const appendEvents = useEventStore((s) => s.appendEvents);
  const setConnectionState = useEventStore((s) => s.setConnectionState);
  const connectionState = useEventStore((s) => s.connectionState);
  const updateTask = useTaskStore((s) => s.updateTask);
  const updateTurn = useTurnStore((s) => s.updateTurn);
  const setStreamingTurn = useTurnStore((s) => s.setStreamingTurn);

  // 保持对当前连接实例的引用，避免重复创建
  const connectionRef = useRef<SSEConnection | null>(null);
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
   * @param taskId - 要监听的任务标识符。
   * @param turnId - 要监听的轮次标识符。
   * @returns Promise<void>，连接调度后 resolve；流式消费在后台继续。
   */
  const connect = useCallback(
    async (taskId: string, turnId: string): Promise<void> => {
      PerfTrace.markCurrent("sse:connect-start", { task_id: taskId, turn_id: turnId });
      // 先 flush 上一连接已入缓冲、尚未到下一动画帧的事件，避免快速重连时静默丢弃
      // （connectionRef 存的是 SSEConnection，其 disconnect 只 abort 不 flush；只有 hook
      // 自身的 disconnect 才 flush，故这里必须显式 flush 而非依赖下方 disconnect）。
      flushRef.current();
      // 再断开已有连接（SSEConnection.disconnect 仅负责 abort fetch，不再产生事件）
      if (connectionRef.current) {
        connectionRef.current.disconnect();
      }
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
        setStreamingTurn(null);
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
        appendEvents(batch);
        for (const event of batch) {
          syncRuntimeStatus(event, updateTask, updateTurn, setStreamingTurn);
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

      const connection = new SSEConnection({
        taskId,
        turnId,
        onEvent,
        onError,
        onStateChange: (state) => {
          if (state === SSEConnectionState.STREAMING) {
            PerfTrace.markCurrent("sse:connection-open", { task_id: taskId, turn_id: turnId });
          }
          // 仅当前活动连接可回写连接状态，避免被已断开的旧连接（竞态）误钉为 CLOSED
          if (connectionRef.current === connection) {
            setConnectionState(state);
          }
        },
      });

      connectionRef.current = connection;
      void connection.connect()
        .finally(() => {
          // 仅当前活动连接可接管清理与终态回写：极边缘场景下旧连接自然异常结束、同时
          // 用户已重连新连接时，旧连接 finally 不得 flush 共享缓冲（会误提新连接事件）
          // 也不得把新进行中的任务误标 failed。
          const isActive = connectionRef.current === connection;
          if (isActive) {
            connectionRef.current = null;
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
    [appendEvents, setConnectionState, setStreamingTurn, updateTask, updateTurn],
  );

  /**
   * 断开当前活跃的 SSE 连接。
   */
  const disconnect = useCallback(() => {
    if (connectionRef.current) {
      connectionRef.current.disconnect();
      connectionRef.current = null;
    }
    // 主动断开时兜底 flush，保证 UI 与最终状态一致
    flushRef.current();
  }, []);

  return { connect, disconnect, connectionState };
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
      setStreamingTurn(null);
    }
  }
}

/**
 * 从 runtime event 推导 task/turn 状态。
 *
 * 终态事件识别范围：run_finished / run_failed / run_cancelled / client_disconnected。
 * 对 run_failed 与 client_disconnected 的语义差异处理：
 *   - run_failed 优先采用 payload.end_reason 作为 endReason（如
 *     "client_disconnected"），其次回退到 payload.error 文案，最后回退枚举值
 *     "run_failed"，使上层 UI 能区分「客户端连接断开」与「Agent 真实执行失败」。
 *   - client_disconnected 裸事件直接识别为终态 failed，endReason="client_disconnected"，
 *     避免 SSE 断开时 UI 永远停在 running/active 误导用户。
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
  // 客户端连接断开：后端经 finally 兜底标记 turn failed 并下发该事件时，需被识别为终态，
  // 否则 UI 会永远停在 running/active，用户误以为对话仍在进行。
  if (event.event_type === "client_disconnected") {
    return { taskStatus: "failed", turnStatus: "failed", terminal: true, endReason: "client_disconnected" };
  }
  return null;
}
