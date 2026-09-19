import { useCallback, useEffect, useRef } from "react";

import { jsonRequestInit, requestRaw } from "@/lib/http/client";
import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { requestAssistantSnapshot } from "@/lib/assistant/assistant-snapshot-client";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

const BUSINESS_RESUME_TIMEOUT_MS = 15_000;

export type BusinessResumeController = {
  resumeBusinessRun: () => Promise<void>;
  cancelBusinessResume: () => void;
};

type BusinessResumeAcceptedStage = "business-started" | "attach-only";

type UncertainBusinessResume = {
  runId: number;
};

/**
 * Own the explicit business-resume request independently from Assistant UI's
 * attach-only transport resume.
 *
 * The POST /assistant request continues the persisted business Run. Once that
 * request is accepted, `onBusinessResumeAccepted` performs the separate
 * attach-only stage. Only one business-resume request may be in flight for a
 * session; stale responses are ignored by the local generation and runtime
 * availability guards.
 */
export function useBusinessResume(
  context: RuntimeSessionContext,
  onBusinessResumeAccepted: (stage: BusinessResumeAcceptedStage) => void,
): BusinessResumeController {
  const inFlightRef = useRef<Promise<void> | null>(null);
  const generationRef = useRef(0);
  const abortControllerRef = useRef<AbortController | null>(null);
  const disposedRef = useRef(false);
  const backendAvailableRef = useRef(context.backendRuntimeAvailable);
  const backendGenerationRef = useRef(context.backendRuntimeGeneration);
  const previousBackendGenerationRef = useRef(context.backendRuntimeGeneration);
  const uncertainResumeRef = useRef<UncertainBusinessResume | null>(null);
  const acceptedBusinessRunIdRef = useRef<number | null>(null);
  const activeRequestRunIdRef = useRef<number | null>(null);
  const phaseRef = useRef<"idle" | "business-starting" | "attach-pending">("idle");
  backendAvailableRef.current = context.backendRuntimeAvailable;
  backendGenerationRef.current = context.backendRuntimeGeneration;

  const cancelBusinessResume = useCallback(() => {
    const runId = activeRequestRunIdRef.current;
    if (!disposedRef.current && inFlightRef.current && runId !== null) {
      uncertainResumeRef.current = {
        runId,
      };
      phaseRef.current = "business-starting";
    }
    generationRef.current += 1;
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
  }, [context.latestStateRef]);

  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      cancelBusinessResume();
    };
  }, [cancelBusinessResume]);

  useEffect(() => {
    if (!context.backendRuntimeAvailable) cancelBusinessResume();
  }, [cancelBusinessResume, context.backendRuntimeAvailable]);

  useEffect(() => {
    const previousGeneration = previousBackendGenerationRef.current;
    previousBackendGenerationRef.current = context.backendRuntimeGeneration;
    if (previousGeneration !== context.backendRuntimeGeneration) cancelBusinessResume();
  }, [cancelBusinessResume, context.backendRuntimeGeneration]);

  const markBusinessResumeAccepted = useCallback((runId: number, stage: BusinessResumeAcceptedStage) => {
    acceptedBusinessRunIdRef.current = runId;
    phaseRef.current = "attach-pending";
    onBusinessResumeAccepted(stage);
  }, [onBusinessResumeAccepted]);

  const reconcileUncertainResume = useCallback(async (runId: number): Promise<boolean> => {
    if (uncertainResumeRef.current?.runId !== runId) return false;
    if (!backendAvailableRef.current) throw new Error("本机后端当前不可用，无法确认继续运行结果");

    const generation = ++generationRef.current;
    const { controller, signal, clear } = createTimeoutAbort(BUSINESS_RESUME_TIMEOUT_MS);
    const requestBackendGeneration = backendGenerationRef.current;
    activeRequestRunIdRef.current = runId;
    phaseRef.current = "business-starting";
    abortControllerRef.current = controller;
    const isCurrent = () => !disposedRef.current
      && !signal.aborted
      && generation === generationRef.current
      && backendAvailableRef.current
      && requestBackendGeneration === backendGenerationRef.current
      && context.latestStateRef.current.current_run_id === runId
      && context.cancelRequestedRunIdRef.current !== runId;
    try {
      const snapshot = await requestAssistantSnapshot(context.taskId, {
        signal,
        traceId: context.traceId,
      });
      if (!isCurrent()) throw new Error("本机后端实例已变化，暂不能确认继续运行结果");

      context.latestStateRef.current = snapshot;
      const currentRun = currentTransportRun(snapshot);
      if (
        currentRun?.runId === runId
        && (currentRun.status === "pending" || currentRun.status === "running")
      ) {
        uncertainResumeRef.current = null;
        markBusinessResumeAccepted(runId, "business-started");
        return true;
      }

      uncertainResumeRef.current = null;
      phaseRef.current = "idle";
      throw new Error("当前运行已不处于可恢复状态，请重新同步");
    } finally {
      clear();
      if (abortControllerRef.current === controller) abortControllerRef.current = null;
      if (activeRequestRunIdRef.current === runId) activeRequestRunIdRef.current = null;
    }
  }, [context, markBusinessResumeAccepted]);

  const resumeBusinessRun = useCallback(() => {
    if (inFlightRef.current) return inFlightRef.current;
    if (disposedRef.current) return Promise.resolve();
    if (!backendAvailableRef.current) {
      return Promise.reject(new Error("本机后端当前不可用，无法继续运行"));
    }

    const runId = context.latestStateRef.current.current_run_id;
    if (runId == null) return Promise.reject(new Error("当前没有可恢复的运行"));
    if (context.cancelRequestedRunIdRef.current === runId) {
      return Promise.reject(new Error("当前运行正在停止确认中，无法继续运行"));
    }

    if (acceptedBusinessRunIdRef.current !== null && acceptedBusinessRunIdRef.current !== runId) {
      acceptedBusinessRunIdRef.current = null;
      phaseRef.current = "idle";
    }
    if (acceptedBusinessRunIdRef.current === runId) {
      const currentRun = currentTransportRun(context.latestStateRef.current);
      if (currentRun?.runId === runId && (currentRun.status === "pending" || currentRun.status === "running")) {
        phaseRef.current = "attach-pending";
        onBusinessResumeAccepted("attach-only");
        return Promise.resolve();
      }
      acceptedBusinessRunIdRef.current = null;
      phaseRef.current = "idle";
    }

    if (uncertainResumeRef.current && uncertainResumeRef.current.runId !== runId) {
      uncertainResumeRef.current = null;
    }
    if (uncertainResumeRef.current?.runId === runId) {
      const trackedReconcile = reconcileUncertainResume(runId)
        .then((attached) => {
          if (!attached && context.latestStateRef.current.current_run_id !== runId) return;
        })
        .finally(() => {
          inFlightRef.current = null;
        });
      inFlightRef.current = trackedReconcile;
      return trackedReconcile;
    }

    const generation = ++generationRef.current;
    const runtimeGeneration = context.backendRuntimeGeneration;
    activeRequestRunIdRef.current = runId;
    phaseRef.current = "business-starting";
    const { controller, signal, clear } = createTimeoutAbort(BUSINESS_RESUME_TIMEOUT_MS);
    abortControllerRef.current = controller;

    const isCurrent = () => !disposedRef.current
      && !controller.signal.aborted
      && generation === generationRef.current
      && backendAvailableRef.current
      && runtimeGeneration === backendGenerationRef.current
      && context.latestStateRef.current.current_run_id === runId
      && context.cancelRequestedRunIdRef.current !== runId;

    const request = (async () => {
      let requestDefinitivelyRejected = false;
      try {
        const response = await requestRaw("/assistant", jsonRequestInit({
          commands: [],
          taskId: context.taskId,
          threadId: `task-${context.taskId}`,
          runId,
          ...(context.workspaceId != null ? { workspaceId: context.workspaceId } : {}),
        }, {
          method: "POST",
          signal,
          traceId: context.traceId,
          headers: { Accept: "text/event-stream" },
        }));
        if (!response.ok) {
          requestDefinitivelyRejected = true;
          throw new Error(`继续运行失败（HTTP ${response.status}）`);
        }
        try {
          await response.body?.cancel();
        } catch (error) {
          // The business request has already been accepted. A local stream
          // cleanup failure must not prevent the attach-only follow-up.
          void frontendLog("WARNING", "assistant_business_resume_body_cancel_failed", "继续运行响应清理失败，将继续尝试只读订阅", {
            traceId: context.traceId,
            data: { taskId: context.taskId, runId },
            error,
          });
        }
        if (isCurrent()) {
          uncertainResumeRef.current = null;
          markBusinessResumeAccepted(runId, "business-started");
        } else if (!disposedRef.current && context.latestStateRef.current.current_run_id === runId) {
          uncertainResumeRef.current = {
            runId,
          };
          phaseRef.current = "business-starting";
        }
      } catch (error) {
        if (!requestDefinitivelyRejected && !disposedRef.current && context.latestStateRef.current.current_run_id === runId) {
          uncertainResumeRef.current = {
            runId,
          };
          phaseRef.current = "business-starting";
        }
        if (controller.signal.aborted || generation !== generationRef.current || disposedRef.current) {
          return;
        }
        throw error;
      } finally {
        clear();
        if (abortControllerRef.current === controller) abortControllerRef.current = null;
        if (activeRequestRunIdRef.current === runId) activeRequestRunIdRef.current = null;
      }
    })();

    const tracked = request.finally(() => {
      if (inFlightRef.current === tracked) inFlightRef.current = null;
    });
    inFlightRef.current = tracked;
    return tracked;
  }, [context, markBusinessResumeAccepted, onBusinessResumeAccepted, reconcileUncertainResume]);

  return { resumeBusinessRun, cancelBusinessResume };
}
