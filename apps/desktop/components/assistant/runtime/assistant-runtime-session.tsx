import { memo, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { type TransportIssue } from "@/components/assistant/transport-status";
import { AssistantRuntimeTransportHost } from "@/components/assistant/runtime/assistant-runtime-transport-host";
import { useRuntimeCancellation } from "@/components/assistant/runtime/use-runtime-cancellation";
import { useRuntimeDiagnostics } from "@/components/assistant/runtime/use-runtime-diagnostics";
import { useBusinessResume } from "@/components/assistant/runtime/use-business-resume";
import { useCancellationConfirmation } from "@/components/assistant/runtime/use-cancellation-confirmation";
import { useRuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import type {
  AssistantRuntimeProps,
  ComposerRestore,
  RuntimeControls,
  RuntimeSessionContext,
} from "@/components/assistant/runtime/runtime-types";
import { createAssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";
import { getToolGroups, toolNamesForGroups, type ToolGroupCatalog } from "@/lib/api/tools";
import {
  getBackendRuntimeSnapshot,
  subscribeBackendRuntime,
} from "@/src/runtime-config";

type RuntimeSessionProps = AssistantRuntimeProps & {
  setIssue: (issue: TransportIssue | null) => void;
};

/** 组装后端 Transport runtime、生命周期协调器和独立的对话渲染宿主。 */
export const AssistantRuntimeSession = memo(function AssistantRuntimeSession({
  taskId,
  workspaceId,
  workspaceRoot,
  initialState,
  setIssue,
  initialMessage,
  initialAttachments,
  initialDisabledToolGroups,
  initialAllowsTools,
  forkAvailable,
  forkingRunId,
  onForkRun,
  onTaskStateChanged,
  onRunStateChange,
}: RuntimeSessionProps) {
  const backendRuntime = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendRuntimeSnapshot,
    getBackendRuntimeSnapshot,
  );
  const {
    backendBaseUrl,
    generation: backendRuntimeGeneration,
    available: backendRuntimeAvailable,
  } = backendRuntime;
  const [traceId] = useState(() => newTraceId());
  const performanceProbe = useMemo(() => {
    if (!import.meta.env.DEV) return null;
    return createAssistantPerformanceProbe({
      enabled: true,
      onReport: (report) => {
        void frontendLog("INFO", "assistant_render_performance_report", "Assistant 开发期渲染性能报告已采集", {
          traceId,
          data: { taskId, ...report },
        });
      },
    });
  }, [taskId, traceId]);
  const [toolGroups, setToolGroups] = useState<ToolGroupCatalog[]>([]);
  const [selectedToolGroups, setSelectedToolGroups] = useState<string[]>(() => initialDisabledToolGroups ?? []);
  const [toolGroupsLoading, setToolGroupsLoading] = useState(true);
  const [toolGroupsError, setToolGroupsError] = useState<string | null>(null);
  const initialStateRef = useRef(initialState);
  const sessionInitialState = initialStateRef.current;
  const latestStateRef = useRef(sessionInitialState);
  const runtimeControlsRef = useRef<RuntimeControls | null>(null);
  const attachTransportRef = useRef<(() => Promise<void>) | null>(null);
  const cancelRequestedRunIdRef = useRef<number | null>(null);
  const lastTransportErrorRef = useRef<TransportIssue | null>(null);
  const composerRestoreRef = useRef<ComposerRestore | null>(null);
  const initialMessageSentRef = useRef(false);
  const onTaskStateChangedRef = useRef(onTaskStateChanged);
  onTaskStateChangedRef.current = onTaskStateChanged;

  useEffect(() => {
    if (!backendRuntimeAvailable) {
      setToolGroupsLoading(true);
      return;
    }
    const controller = new AbortController();
    setToolGroupsLoading(true);
    setToolGroupsError(null);
    void getToolGroups({ signal: controller.signal })
      .then(({ groups }) => setToolGroups(groups))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setToolGroupsError(error instanceof Error ? error.message : "工具分组加载失败");
      })
      .finally(() => {
        if (!controller.signal.aborted) setToolGroupsLoading(false);
      });
    return () => controller.abort();
  }, [backendRuntimeAvailable, backendRuntimeGeneration]);

  const selectedAllowsTools = useMemo(() => {
    if (toolGroupsLoading || toolGroupsError) return initialAllowsTools ?? [];
    const disabledTools = new Set(toolNamesForGroups(selectedToolGroups, toolGroups));
    return toolGroups
      .flatMap(({ tools }) => tools)
      .map(({ name }) => name)
      .filter((name) => !disabledTools.has(name));
  }, [initialAllowsTools, selectedToolGroups, toolGroups, toolGroupsError, toolGroupsLoading]);

  const registerRuntimeControls = useCallback((controls: RuntimeControls | null) => {
    runtimeControlsRef.current = controls;
  }, []);
  const registerComposerRestore = useCallback((restore: ComposerRestore) => {
    composerRestoreRef.current = restore;
  }, []);
  const handleInitialMessageError = useCallback((message: string) => {
    setIssue({ message, canResync: true });
  }, [setIssue]);
  const notifyTaskStateChanged = useCallback(() => {
    onTaskStateChangedRef.current?.();
  }, []);

  const context = useMemo<RuntimeSessionContext>(() => ({
    taskId,
    workspaceId,
    workspaceRoot,
    initialState: sessionInitialState,
    backendBaseUrl,
    backendRuntimeGeneration,
    backendRuntimeAvailable,
    traceId,
    performanceProbe,
    setIssue,
    onTaskStateChanged: notifyTaskStateChanged,
    latestStateRef,
    runtimeControlsRef,
    attachTransportRef,
    cancelRequestedRunIdRef,
    lastTransportErrorRef,
    composerRestoreRef,
  }), [
    backendBaseUrl,
    backendRuntimeGeneration,
    backendRuntimeAvailable,
    notifyTaskStateChanged,
    performanceProbe,
    sessionInitialState,
    setIssue,
    taskId,
    traceId,
    workspaceId,
    workspaceRoot,
  ]);

  useRuntimeDiagnostics(context);
  const cancellationConfirmation = useCancellationConfirmation(context);
  const recovery = useRuntimeRecovery(context, cancellationConfirmation.hasActiveConfirmation);
  const finishBusinessResume = useCallback((stage: "business-started" | "attach-only") => {
    if (stage === "business-started") recovery.resetTransportRecoveryBudget();
    runtimeControlsRef.current?.resume();
  }, [recovery.resetTransportRecoveryBudget]);
  const businessResume = useBusinessResume(context, finishBusinessResume);
  const cancellation = useRuntimeCancellation(context, {
    ...cancellationConfirmation,
    reconcileAfterTransportFinish: recovery.reconcileAfterTransportFinish,
  });
  const commitTransportState = useCallback((state: typeof sessionInitialState) => {
    latestStateRef.current = state;
    cancellation.onStateCommitted(state);
  }, [cancellation.onStateCommitted, sessionInitialState]);
  return (
    <AssistantRuntimeTransportHost
      context={context}
      recovery={recovery}
      commitTransportState={commitTransportState}
      selectedAllowsTools={selectedAllowsTools}
      registerRuntimeControls={registerRuntimeControls}
      registerComposerRestore={registerComposerRestore}
      initialMessageSentRef={initialMessageSentRef}
      toolGroups={toolGroups}
      selectedToolGroups={selectedToolGroups}
      onSelectedToolGroupsChange={setSelectedToolGroups}
      toolGroupsLoading={toolGroupsLoading}
      toolGroupsError={toolGroupsError}
      initialMessage={initialMessage}
      initialAttachments={initialAttachments}
      forkAvailable={forkAvailable}
      forkingRunId={forkingRunId}
      onForkRun={onForkRun}
      onRunStateChange={onRunStateChange}
      onResumeBusiness={businessResume.resumeBusinessRun}
      onInitialMessageError={handleInitialMessageError}
      onCancelRequested={cancellation.onRequested}
      onCancelResult={cancellation.onResult}
      cancellingRunId={cancellation.cancellingRunId}
      performanceProbe={performanceProbe}
    />
  );
});
