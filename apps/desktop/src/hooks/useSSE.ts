/**
 * SSE 事件流连接 Hook。
 *
 * 封装 SSE 连接的完整生命周期：
 * - 连接 / 断开 / 重连
 * - 事件分发到 eventStore（含回放去重）
 * - 错误处理与状态管理
 *
 * 组件只需调用 connect(taskId) 即可开始接收事件流。
 *
 * @module hooks/useSSE
 */

import { useCallback, useRef } from "react";
import { useEventStore } from "../stores/eventStore";
import { SSEConnection, type SSEErrorHandler, type SSEConnectionState } from "../services/sse";
import { logError } from "../lib/logger";

/**
 * SSE 事件流 Hook 返回值接口。
 */
interface UseSSEReturn {
  /**
   * 建立 SSE 连接并开始后台接收指定任务的事件流。
   *
   * @param taskId - 要监听的任务 ID。
   * @returns Promise<void>，连接启动后 resolve；后续流错误通过日志和状态回调处理。
   */
  connect: (taskId: string) => Promise<void>;

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
  const appendEvent = useEventStore((s) => s.appendEvent);
  const setConnectionState = useEventStore((s) => s.setConnectionState);
  const connectionState = useEventStore((s) => s.connectionState);

  // 保持对当前连接实例的引用，避免重复创建
  const connectionRef = useRef<SSEConnection | null>(null);

  /**
   * 建立到指定任务的 SSE 连接。
   *
   * @param taskId - 要监听的任务标识符。
   * @returns Promise<void>，连接启动后 resolve；流式消费在后台继续。
   */
  const connect = useCallback(
    async (taskId: string): Promise<void> => {
      // 先断开已有连接
      if (connectionRef.current) {
        connectionRef.current.disconnect();
      }

      const onError: SSEErrorHandler = (error: Error) => {
        logError("SSE 连接错误", error, { module: "useSSE" });
      };

      const connection = new SSEConnection({
        taskId,
        onEvent: appendEvent,
        onError,
        onStateChange: setConnectionState,
      });

      connectionRef.current = connection;
      void connection.connect().catch(() => {
        // SSEConnection 已通过 onError 和内部日志记录错误，这里只负责避免未处理 Promise。
      });
    },
    [appendEvent, setConnectionState],
  );

  /**
   * 断开当前活跃的 SSE 连接。
   */
  const disconnect = useCallback(() => {
    if (connectionRef.current) {
      connectionRef.current.disconnect();
      connectionRef.current = null;
    }
  }, []);

  return { connect, disconnect, connectionState };
}
