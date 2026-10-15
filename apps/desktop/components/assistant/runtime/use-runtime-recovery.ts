import { useCallback, useEffect, useRef } from "react";

import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import {
  currentTransportRun,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import type { TransportState } from "@/lib/assistant/contract";
import type { TransportIssue } from "@/components/assistant/transport-status";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

export type RecoveryReason = "transport_finished" | "retry_after_failure" | "user_resync" | "backend_restored";

export type RuntimeRecovery = {
  reconcileAfterTransportFinish: (target?: { runId: number | null; status: string | null }) => Promise<void>;
  /** 连接已建立或新命令开始：连续失败计数清零，取消待执行的恢复。 */
  resetTransportRecoveryBudget: () => void;
  /**
   * 显式原地重新对齐（「重新同步」按钮、后端恢复）：清零预算后立即读取 canonical
   * snapshot；运行中则继续订阅，已结束则导入快照。会话、草稿与附件都不卸载。
   */
  resync: (reason: "user_resync" | "backend_restored", issue?: TransportIssue | null) => void;
  /** 后端不可用：停止计时器与在途读取，等待 resync("backend_restored")。 */
  suspend: () => void;
};

/**
 * 有界恢复只统计「连续」失败：每次成功建立连接都会经 resetTransportRecoveryBudget
 * 清零，长 Run 中偶发的断流（如 backpressure 触发的 resync_required EOF）不会累积耗尽预算。
 */
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAYS_MS = [0, 250, 500, 1_000, 2_000] as const;
const RECONCILE_TIMEOUT_MS = 15_000;

function findRun(state: TransportState, runId: number | null) {
  return runId === null ? undefined : state.runs.find((run) => run.runId === runId);
}

function isActiveStatus(status: string | null | undefined): boolean {
  return status === "pending" || status === "running";
}

/** 会话内唯一的 Transport snapshot 对账编排：断流恢复、手动重新同步与后端恢复都走这里。 */
export function useRuntimeRecovery(
  context: RuntimeSessionContext,
  hasActiveCancellation?: () => boolean,
): RuntimeRecovery {
  const reconnectAttemptRef = useRef(0);
  const reconcileInFlightRef = useRef(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconcileAbortControllerRef = useRef<AbortController | null>(null);
  const recoveryGenerationRef = useRef(0);
  const disposedRef = useRef(false);

  const resetTransportRecoveryBudget = useCallback(() => {
    recoveryGenerationRef.current += 1;
    reconnectAttemptRef.current = 0;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    reconcileAbortControllerRef.current?.abort();
    reconcileInFlightRef.current = false;
  }, []);

  useEffect(() => {
    // React StrictMode replays effects on the mounted hook instance in
    // development. Restore the live flag in every setup, not only on mount.
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      reconcileAbortControllerRef.current?.abort();
    };
  }, []);

  /** 计时器触发时仍属于同一轮恢复、同一后端实例才继续。 */
  const scheduleOnSameBackend = useCallback((delay: number, action: () => void) => {
    const scheduledGeneration = recoveryGenerationRef.current;
    const scheduledBackendGeneration = context.backendRef.current.generation;
    reconnectTimerRef.current = window.setTimeout(() => {
      reconnectTimerRef.current = null;
      if (
        disposedRef.current
        || !context.backendRef.current.available
        || scheduledGeneration !== recoveryGenerationRef.current
        || scheduledBackendGeneration !== context.backendRef.current.generation
      ) return;
      action();
    }, delay);
  }, [context.backendRef]);

  const reconcile = useCallback(async function reconcile(
    reason: RecoveryReason,
    target?: { runId: number | null; status: string | null },
  ): Promise<void> {
    if (hasActiveCancellation?.()) return;
    if (
      disposedRef.current
      || !context.backendRef.current.available
      || reconcileInFlightRef.current
      || reconnectTimerRef.current !== null
    ) return;
    if (reconnectAttemptRef.current >= MAX_RECONNECT_ATTEMPTS) {
      context.setIssue({ message: "本机后端连接多次中断，请重新同步恢复对话。", canResync: true });
      return;
    }

    const attempt = reconnectAttemptRef.current;
    const generation = recoveryGenerationRef.current;
    const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
    reconnectAttemptRef.current += 1;

    const knownRun = target?.runId === null || target?.runId === undefined
      ? currentTransportRun(context.latestStateRef.current)
      : findRun(context.latestStateRef.current, target.runId);
    const knownStatus = target?.status ?? knownRun?.status ?? null;
    void frontendLog("INFO", "assistant_transport_reconcile_started", "Assistant Transport 开始有界恢复", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        reason,
        lastRunId: context.latestStateRef.current.current_run_id,
        lastRunStatus: knownStatus,
        attempt: attempt + 1,
      },
    });

    // 流结束时已知 Run 仍在运行：直接重新 attach，不必先读快照。
    if (reason === "transport_finished" && knownRun && isActiveStatus(knownStatus)) {
      const scheduledRunId = target?.runId ?? knownRun.runId;
      scheduleOnSameBackend(delay, () => {
        if (context.cancelRequestedRunIdRef.current === scheduledRunId) return;
        const latestRun = findRun(context.latestStateRef.current, scheduledRunId);
        if (!latestRun || !isActiveStatus(latestRun.status)) return;
        void context.attachTransportRef.current?.();
      });
      void frontendLog("INFO", "assistant_transport_reconcile_attach_scheduled", "已安排原子 attach 恢复流", {
        traceId: context.traceId,
        data: { taskId: context.taskId, runId: scheduledRunId, delay, attempt: attempt + 1 },
      });
      return;
    }

    reconcileInFlightRef.current = true;
    const controller = new AbortController();
    reconcileAbortControllerRef.current = controller;
    try {
      const outcome = await fetchCanonicalSnapshot(context.taskId, {
        reason,
        timeoutMs: RECONCILE_TIMEOUT_MS,
        traceId: context.traceId,
        signal: controller.signal,
      });
      // superseded：后端已变化或本轮被取代，由后端恢复入口 / 新一轮恢复负责对齐。
      if (
        outcome.kind === "superseded"
        || disposedRef.current
        || hasActiveCancellation?.()
        || generation !== recoveryGenerationRef.current
      ) return;
      const snapshot = outcome.snapshot;
      context.latestStateRef.current = snapshot;
      const current = currentTransportRun(snapshot);
      void frontendLog("INFO", "assistant_transport_reconcile_completed", "Assistant Transport 最新快照已读取", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          reason,
          runId: snapshot.current_run_id,
          runStatus: current?.status ?? null,
          messageCount: transportMessageCount(snapshot),
        },
      });

      // 运行中的 Run 由 resume 重新订阅，订阅流会带回完整状态；只有终态才直接导入快照。
      if (isActiveStatus(current?.status)) {
        const scheduledRunId = snapshot.current_run_id;
        scheduleOnSameBackend(delay, () => {
          if (scheduledRunId !== null && context.cancelRequestedRunIdRef.current === scheduledRunId) return;
          const latestRun = currentTransportRun(context.latestStateRef.current);
          if (!latestRun || latestRun.runId !== scheduledRunId || !isActiveStatus(latestRun.status)) return;
          context.runtimeControlsRef.current?.resume();
        });
        return;
      }

      resetTransportRecoveryBudget();
      context.runtimeControlsRef.current?.importState(snapshot);
      context.setIssue(null);
    } catch (error) {
      // fetchCanonicalSnapshot 已把取消与后端变化收敛为 superseded；到这里的都是真实失败（含超时）。
      if (
        disposedRef.current
        || hasActiveCancellation?.()
        || generation !== recoveryGenerationRef.current
      ) return;
      context.setIssue({
        message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
        canResync: true,
      });
      if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
        scheduleOnSameBackend(delay, () => {
          if (hasActiveCancellation?.()) return;
          void reconcile("retry_after_failure");
        });
      }
    } finally {
      if (reconcileAbortControllerRef.current === controller) {
        reconcileAbortControllerRef.current = null;
      }
      if (generation === recoveryGenerationRef.current) reconcileInFlightRef.current = false;
    }
  }, [context, hasActiveCancellation, resetTransportRecoveryBudget, scheduleOnSameBackend]);

  const reconcileAfterTransportFinish = useCallback(
    (target?: { runId: number | null; status: string | null }) => reconcile("transport_finished", target),
    [reconcile],
  );

  const resync = useCallback((reason: "user_resync" | "backend_restored", issue?: TransportIssue | null) => {
    resetTransportRecoveryBudget();
    if (issue !== undefined) context.setIssue(issue);
    void reconcile(reason);
  }, [context, reconcile, resetTransportRecoveryBudget]);

  const suspend = useCallback(() => {
    resetTransportRecoveryBudget();
  }, [resetTransportRecoveryBudget]);

  return {
    reconcileAfterTransportFinish,
    resetTransportRecoveryBudget,
    resync,
    suspend,
  };
}
