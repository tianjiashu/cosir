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
  "final_response",
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
 * 判断 delegation 是否无需继续订阅。
 *
 * @param descriptor - 委派订阅描述。
 * @returns delegation 自身终态或 child run 终态时返回 true。
 *
 * @sideeffect 无。
 */
function isTerminalDescriptor(descriptor: DelegationStreamDescriptor): boolean {
  return descriptor.delegationTerminal || descriptor.childTerminal;
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

  useEffect(() => {
    const connections = connectionsRef.current;
    const failedDelegations = failedDelegationsRef.current;
    const missingTerminalBackfilled = missingTerminalBackfilledRef.current;
    const backfillInFlight = backfillInFlightRef.current;
    return () => {
      for (const connection of connections.values()) {
        connection.disconnect();
      }
      connections.clear();
      failedDelegations.clear();
      missingTerminalBackfilled.clear();
      backfillInFlight.clear();
    };
  }, [taskId]);

  useEffect(() => {
    if (!taskId) {
      return;
    }

    const forceBackfill = async (delegationId: string, reason: string): Promise<void> => {
      if (backfillInFlightRef.current.has(delegationId)) {
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
      }
    };

    const descriptors = deriveDelegationStreams(taskEvents);
    const activeChildTurnIds = new Set<string>();

    for (const descriptor of descriptors) {
      if (isTerminalDescriptor(descriptor)) {
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
          appendEvents([event]);
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
          void forceBackfill(descriptor.delegationId, error.message);
        },
      });

      connectionsRef.current.set(descriptor.childTurnId, connection);
      void connection.connect()
        .catch(() => {
          // service 已记录错误并通过 onError 触发强制 backfill；此处只吞掉后台 Promise。
        })
        .finally(() => {
          if (connectionsRef.current.get(descriptor.childTurnId) === connection) {
            connectionsRef.current.delete(descriptor.childTurnId);
          }
        });
    }

    for (const [childTurnId, connection] of connectionsRef.current) {
      if (!activeChildTurnIds.has(childTurnId)) {
        connection.disconnect();
        connectionsRef.current.delete(childTurnId);
      }
    }
  }, [appendEvents, setEvents, taskEvents, taskId]);
}
