/**
 * 委派 child turn 事件流订阅 Hook。
 *
 * 从当前 task 的 runtime events 中发现带 child_turn_id 的未终态 delegation，
 * 为每个 child turn 建立 subscribe-only SSE 连接，并把 child events 合并进同一个
 * eventStore。child stream 的状态只保存在本 hook 实例内，不写全局父 SSE 连接状态。
 *
 * @module hooks/useDelegationStreams
 */

import { useEffect, useRef } from "react";
import type { RuntimeEvent } from "@shared/events";
import { logError, logWarn } from "@/lib/logger";
import { DelegationStreamConnection } from "@/services/delegationStream";
import * as api from "@/services/api";
import { EMPTY_EVENTS, useEventStore } from "@/stores/eventStore";

/** 委派生命周期事件类型。 */
const DELEGATION_EVENT_TYPES = new Set<RuntimeEvent["event_type"]>([
  "delegation_started",
  "delegation_child_started",
  "delegation_finished",
  "delegation_failed",
  "delegation_cancelled",
]);

/** 委派终态事件类型。 */
const TERMINAL_DELEGATION_EVENT_TYPES = new Set<RuntimeEvent["event_type"]>([
  "delegation_finished",
  "delegation_failed",
  "delegation_cancelled",
]);

/** child run 终态事件类型。 */
const TERMINAL_CHILD_EVENT_TYPES = new Set<RuntimeEvent["event_type"]>([
  "run_finished",
  "run_failed",
  "run_cancelled",
]);

/** 从事件流派生出的单个委派订阅描述。 */
interface DelegationStreamDescriptor {
  /** 委派记录标识。 */
  delegationId: string;
  /** child turn 标识。 */
  childTurnId: string;
  /** delegation 自身是否已终态。 */
  delegationTerminal: boolean;
  /** child turn 是否已有 run 终态事件。 */
  childTerminal: boolean;
}

/**
 * 判断事件是否携带可用于 child stream 的委派 payload。
 *
 * @param event - 待判定运行时事件。
 * @returns 是委派事件时返回 true。
 *
 * @sideeffect 无。
 */
function isDelegationEvent(event: RuntimeEvent): boolean {
  return DELEGATION_EVENT_TYPES.has(event.event_type);
}

/**
 * 从未知 payload 字段读取非空字符串。
 *
 * @param payload - 事件 payload。
 * @param key - 待读取字段名。
 * @returns 非空字符串字段；不存在或非字符串时返回 null。
 *
 * @sideeffect 无。
 */
function readStringPayload(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * 从 task 事件流派生需要管理的 delegation child stream。
 *
 * @param events - 当前 task 的完整事件缓存。
 * @returns 按出现顺序去重后的 delegation stream 描述。
 *
 * @sideeffect 无。
 */
function deriveDelegationStreams(events: RuntimeEvent[]): DelegationStreamDescriptor[] {
  const descriptors = new Map<string, DelegationStreamDescriptor>();
  const childTurnIdsByDelegationId = new Map<string, string>();

  for (const event of events) {
    if (!isDelegationEvent(event)) {
      continue;
    }
    const payload = event.payload as Record<string, unknown>;
    const delegationId = readStringPayload(payload, "delegation_id");
    const childTurnId = readStringPayload(payload, "child_turn_id");
    if (!delegationId || !childTurnId) {
      continue;
    }
    childTurnIdsByDelegationId.set(delegationId, childTurnId);
    const existing = descriptors.get(delegationId);
    descriptors.set(delegationId, {
      delegationId,
      childTurnId,
      delegationTerminal: Boolean(existing?.delegationTerminal) || TERMINAL_DELEGATION_EVENT_TYPES.has(event.event_type),
      childTerminal: Boolean(existing?.childTerminal),
    });
  }

  for (const event of events) {
    if (!event.turn_id || !TERMINAL_CHILD_EVENT_TYPES.has(event.event_type)) {
      continue;
    }
    for (const [delegationId, childTurnId] of childTurnIdsByDelegationId) {
      if (event.turn_id !== childTurnId) {
        continue;
      }
      const existing = descriptors.get(delegationId);
      if (existing) {
        descriptors.set(delegationId, { ...existing, childTerminal: true });
      }
    }
  }

  return [...descriptors.values()];
}

/**
 * 判断 child SSE 连接是否应以 child run 终态为准断开。
 *
 * 断开时机只依据 child run 终态（`childTerminal`）。delegation 级终态事件
 * （`delegation_finished/failed/cancelled`）是更高层的汇总事件，**不控制**底层
 * child 事件流的生命周期：它到达时 child run 事件可能仍滞后，因此不得据此 disconnect，
 * 否则会丢失滞后到达的 child run 事件，只能依赖 forceBackfill 兜底且存在丢失窗口。
 *
 * @param descriptor - 委派订阅描述。
 * @returns child turn 已有 run 终态事件（`run_finished/run_failed/run_cancelled`）时返回 true。
 *
 * @sideeffect 无。
 */
function isChildRunTerminalDescriptor(descriptor: DelegationStreamDescriptor): boolean {
  return descriptor.childTerminal;
}

/**
 * 委派 child turn 事件流订阅 Hook。
 *
 * @param taskId - 当前活跃任务标识；为空时断开并不订阅。
 * @returns 无。
 *
 * @sideeffect
 * - 为未终态 delegation 创建 `/turns/{child_turn_id}/events/stream` 连接
 * - 将 child stream 事件写入 eventStore.appendEvents
 * - child SSE 连接的断开以 **child run 终态**（`run_finished/run_failed/run_cancelled`）
 *   为准；delegation 级终态事件（`delegation_finished/failed/cancelled`）是高层汇总事件，
 *   不触发 disconnect，避免丢失其之后滞后到达的 child run 事件
 * - child stream 错误时强制调用 `api.listTaskEvents(taskId)` 并 `setEvents`
 */
export function useDelegationStreams(taskId: string | null): void {
  const taskEvents = useEventStore((state) => (taskId ? state.eventsByTaskId[taskId] ?? EMPTY_EVENTS : EMPTY_EVENTS));
  const appendEvents = useEventStore((state) => state.appendEvents);
  const setEvents = useEventStore((state) => state.setEvents);
  const connectionsRef = useRef<Map<string, DelegationStreamConnection>>(new Map());
  const failedDelegationsRef = useRef<Set<string>>(new Set());
  const missingTerminalBackfilledRef = useRef<Set<string>>(new Set());
  const backfillInFlightRef = useRef<Set<string>>(new Set());
  const forcedBackfillPendingRef = useRef<Map<string, string>>(new Map());
  // 攒批缓冲：对齐父流 useSSE 的 rAF 攒批模式。把同一动画帧内的多个 child 事件合并成
  // 一次 appendEvents + 一次渲染，将高频 child 流下的 set/投影/重渲染压力从「每事件一次」
  // 降到「每帧一次」。多并发 child 同时流式时，单条直写会让每个 child 事件都触发一次
  // state.events 引用变更、触发 SubagentPanel 的 O(N) 全扫描派生；攒批是消除该放大器
  // 的最小且对齐既有实现的做法。
  const pendingEventsRef = useRef<RuntimeEvent[]>([]);
  const rafRef = useRef<number | null>(null);

  /**
   * 把缓冲中的 child 事件一次性提交到 eventStore。
   *
   * 先取消已排程的 rAF/退化定时器，再取出当前 batch，空 batch 直接返回；
   * 非空则清空缓冲并调用一次 appendEvents(batch)（单次 set、单次渲染），
   * 避免每个 child 事件单独触发一次 store 更新与下游派生重算。
   *
   * @returns 无。
   * @throws 不主动抛出；appendEvents 的内部异常会原样向上传播由调用方处理。
   * @sideeffect
   * - 取消 rafRef 持有的 rAF 或 setTimeout 句柄并置空
   * - 取出并重置 pendingEventsRef.current
   * - 调用 appendEvents 将批量事件写入 eventStore
   */
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
  };

  /**
   * 安排下一动画帧执行 flush；若已有排程则幂等返回。
   *
   * 优先使用 requestAnimationFrame；当运行环境不存在 rAF（部分非浏览器上下文）
   * 时退化为 setTimeout(run, 16)。run 执行时先清空 rafRef 再调用 flush，
   * 保证本次帧后无残留句柄、后续新事件可重新排程。
   *
   * @returns 无。
   * @throws 不主动抛出；排程失败由各环境 API 自身行为决定。
   * @sideeffect
   * - 在 rafRef 上登记一个 requestAnimationFrame 或 setTimeout 句柄（首次排程时）
   */
  const scheduleFlush = () => {
    if (rafRef.current != null) {
      return;
    }
    const run = () => {
      rafRef.current = null;
      flush();
    };
    if (typeof requestAnimationFrame === "function") {
      rafRef.current = requestAnimationFrame(run);
    } else {
      rafRef.current = setTimeout(run, 16) as unknown as number;
    }
  };

  useEffect(() => {
    const connections = connectionsRef.current;
    const failedDelegations = failedDelegationsRef.current;
    const missingTerminalBackfilled = missingTerminalBackfilledRef.current;
    const backfillInFlight = backfillInFlightRef.current;
    const forcedBackfillPending = forcedBackfillPendingRef.current;
    return () => {
      // 依赖变更导致连接重建前，先 flush 残留 child 事件缓冲，避免滞留在
      // pendingEventsRef 中尚未到下一动画帧的事件因 connections 断开/task 切换而丢失。
      flush();
      for (const connection of connections.values()) {
        connection.disconnect();
      }
      connections.clear();
      failedDelegations.clear();
      missingTerminalBackfilled.clear();
      backfillInFlight.clear();
      forcedBackfillPending.clear();
    };
  }, [taskId]);

  useEffect(() => {
    if (!taskId) {
      return;
    }

    const forceBackfill = async (delegationId: string, reason: string, forced = false): Promise<void> => {
      if (backfillInFlightRef.current.has(delegationId)) {
        if (forced) {
          forcedBackfillPendingRef.current.set(delegationId, reason);
        }
        return;
      }
      backfillInFlightRef.current.add(delegationId);
      try {
        const events = await api.listTaskEvents(taskId);
        setEvents(events, taskId);
      } catch (err) {
        logError("delegation child stream backfill failed", err, {
          module: "useDelegationStreams",
          task_id: taskId,
          delegation_id: delegationId,
          reason,
        });
      } finally {
        backfillInFlightRef.current.delete(delegationId);
        const pendingForcedReason = forcedBackfillPendingRef.current.get(delegationId);
        if (pendingForcedReason) {
          forcedBackfillPendingRef.current.delete(delegationId);
          void forceBackfill(delegationId, pendingForcedReason, true);
        }
      }
    };

    const descriptors = deriveDelegationStreams(taskEvents);
    const activeChildTurnIds = new Set<string>();

    for (const descriptor of descriptors) {
      if (isChildRunTerminalDescriptor(descriptor)) {
        const connection = connectionsRef.current.get(descriptor.childTurnId);
        if (connection) {
          connection.disconnect();
          connectionsRef.current.delete(descriptor.childTurnId);
        }
        continue;
      }

      activeChildTurnIds.add(descriptor.childTurnId);
      if (!missingTerminalBackfilledRef.current.has(descriptor.delegationId)) {
        missingTerminalBackfilledRef.current.add(descriptor.delegationId);
        void forceBackfill(descriptor.delegationId, "missing_terminal_event");
      }
      if (failedDelegationsRef.current.has(descriptor.delegationId)) {
        continue;
      }
      if (connectionsRef.current.has(descriptor.childTurnId)) {
        continue;
      }

      const connection = new DelegationStreamConnection({
        taskId,
        delegationId: descriptor.delegationId,
        childTurnId: descriptor.childTurnId,
        onEvent: (event) => {
          pendingEventsRef.current.push(event);
          scheduleFlush();
        },
        onError: (error) => {
          failedDelegationsRef.current.add(descriptor.delegationId);
          logWarn("delegation child stream requires task event backfill", {
            module: "useDelegationStreams",
            task_id: taskId,
            delegation_id: descriptor.delegationId,
            child_turn_id: descriptor.childTurnId,
            error: error.message,
          });
          void forceBackfill(descriptor.delegationId, error.message, true);
        },
      });

      connectionsRef.current.set(descriptor.childTurnId, connection);
      void connection.connect()
        .catch((connectError: unknown) => {
          // service 内部已通过 onError 触发强制 backfill，但此处不可静默吞错：
          // 空 catch 违反「禁止空 catch」铁律且无排查线索。补日志确保连接级失败可定位。
          logWarn("delegation child stream connect rejected", {
            module: "useDelegationStreams",
            task_id: taskId,
            delegation_id: descriptor.delegationId,
            child_turn_id: descriptor.childTurnId,
            error: connectError instanceof Error ? connectError.message : String(connectError),
          });
        })
        .finally(() => {
          if (connectionsRef.current.get(descriptor.childTurnId) === connection) {
            connectionsRef.current.delete(descriptor.childTurnId);
          }
        });
    }

    // 断开不再活跃的旧连接前，先 flush 残留 child 事件缓冲：本 effect 重跑时旧的
    // child 连接即将断开、不再产生事件，但 pendingEventsRef 中可能仍有尚未到下一帧
    // 的缓冲事件，需在此兜底提交，避免事件丢失。
    flush();
    for (const [childTurnId, connection] of connectionsRef.current) {
      if (!activeChildTurnIds.has(childTurnId)) {
        connection.disconnect();
        connectionsRef.current.delete(childTurnId);
      }
    }
  }, [appendEvents, setEvents, taskEvents, taskId]);
}
