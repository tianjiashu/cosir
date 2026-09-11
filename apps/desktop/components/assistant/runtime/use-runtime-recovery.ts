import { useCallback, useEffect, useRef } from "react";

import { requestJson, requestRaw } from "@/lib/http/client";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import {
  currentTransportRun,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

type RuntimeRecovery = {
  reconcileAfterTransportFinish: () => Promise<void>;
  resumeBusinessRun: () => Promise<void>;
  resetRecovery: () => void;
};

const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAYS_MS = [0, 250, 500, 1_000, 2_000] as const;
const RECONCILE_TIMEOUT_MS = 15_000;
const BUSINESS_RESUME_TIMEOUT_MS = 15_000;

/** Own backend URL changes, bounded transport reconciliation, and business resume. */
export function useRuntimeRecovery(
  context: RuntimeSessionContext,
): RuntimeRecovery {
  const previousBackendBaseUrlRef = useRef(context.backendBaseUrl);
  const reconnectAttemptRef = useRef(0);
  const reconcileInFlightRef = useRef(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconcileAbortControllerRef = useRef<AbortController | null>(null);
  const recoveryGenerationRef = useRef(0);
  const disposedRef = useRef(false);

  const resetRecovery = useCallback(() => {
    recoveryGenerationRef.current += 1;
    reconnectAttemptRef.current = 0;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    reconcileAbortControllerRef.current?.abort();
    reconcileInFlightRef.current = false;
  }, []);

  useEffect(() => () => {
    disposedRef.current = true;
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    reconcileAbortControllerRef.current?.abort();
  }, []);

  const reconcileAfterTransportFinish = useCallback(async () => {
    if (disposedRef.current || reconcileInFlightRef.current || reconnectTimerRef.current !== null) return;
    if (reconnectAttemptRef.current >= MAX_RECONNECT_ATTEMPTS) {
      context.setIssue({ message: "本机后端连接多次中断，请重试恢复对话。", retryable: true });
      return;
    }

    const attempt = reconnectAttemptRef.current;
    const generation = recoveryGenerationRef.current;
    reconnectAttemptRef.current += 1;
    reconcileInFlightRef.current = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), RECONCILE_TIMEOUT_MS);
    reconcileAbortControllerRef.current = controller;
    try {
      void frontendLog("INFO", "assistant_transport_reconcile_started", "Assistant Transport 流结束后读取最新快照", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          lastRunId: context.latestStateRef.current.current_run_id,
          lastRunStatus: currentTransportRun(context.latestStateRef.current)?.status ?? null,
          attempt: attempt + 1,
        },
      });

      const snapshot = parseTransportState(await requestJson<unknown>(`/tasks/${context.taskId}/assistant/state`, {
        signal: controller.signal,
        traceId: context.traceId,
      }));
      if (disposedRef.current || controller.signal.aborted || generation !== recoveryGenerationRef.current) return;
      context.latestStateRef.current = snapshot;

      void frontendLog("INFO", "assistant_transport_reconcile_completed", "Assistant Transport 最新快照已读取", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          runId: snapshot.current_run_id,
          runStatus: currentTransportRun(snapshot)?.status ?? null,
          messageCount: transportMessageCount(snapshot),
        },
      });

      if (disposedRef.current || generation !== recoveryGenerationRef.current) return;
      const status = currentTransportRun(snapshot)?.status;
      if (status === "pending" || status === "running") {
        const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          if (disposedRef.current) return;
          context.runtimeControlsRef.current?.resume();
        }, delay);
        return;
      }

      resetRecovery();
      context.runtimeControlsRef.current?.importState(snapshot);
      context.setIssue(null);
    } catch (error) {
      if (disposedRef.current || controller.signal.aborted || generation !== recoveryGenerationRef.current) return;
      context.setIssue({
        message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
        retryable: true,
      });
      if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
        const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          void reconcileAfterTransportFinish();
        }, delay);
      }
    } finally {
      window.clearTimeout(timeout);
      if (reconcileAbortControllerRef.current === controller) {
        reconcileAbortControllerRef.current = null;
      }
      if (generation === recoveryGenerationRef.current) reconcileInFlightRef.current = false;
    }
  }, [context, resetRecovery]);

  useEffect(() => {
    const previousBaseUrl = previousBackendBaseUrlRef.current;
    previousBackendBaseUrlRef.current = context.backendBaseUrl;
    if (previousBaseUrl === context.backendBaseUrl) return;

    void frontendLog("INFO", "assistant_backend_runtime_url_changed", "Assistant 后端地址已变化", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        previousBaseUrl,
        backendBaseUrl: context.backendBaseUrl,
      },
    });

    const status = currentTransportRun(context.latestStateRef.current)?.status;
    if (status === "pending" || status === "running") {
      resetRecovery();
      context.setIssue({ message: "本机后端已重启，正在恢复当前对话…", retryable: true });
      void reconcileAfterTransportFinish();
    }
  }, [context, reconcileAfterTransportFinish, resetRecovery]);

  const resumeBusinessRun = useCallback(async () => {
    const runId = context.latestStateRef.current.current_run_id;
    if (runId == null) throw new Error("当前没有可恢复的运行");

    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), BUSINESS_RESUME_TIMEOUT_MS);
    try {
      const response = await requestRaw("/assistant", {
        method: "POST",
        signal: controller.signal,
        traceId: context.traceId,
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          commands: [],
          taskId: context.taskId,
          threadId: `task-${context.taskId}`,
          runId,
          ...(context.workspaceId != null ? { workspaceId: context.workspaceId } : {}),
        }),
      });
      if (!response.ok) throw new Error(`继续运行失败（HTTP ${response.status}）`);
      await response.body?.cancel();
    } finally {
      window.clearTimeout(timeout);
    }
    resetRecovery();
    context.runtimeControlsRef.current?.resume();
  }, [context, resetRecovery]);

  return { reconcileAfterTransportFinish, resumeBusinessRun, resetRecovery };
}

export type { RuntimeRecovery };
