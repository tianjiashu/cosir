import { memo, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";

import { type TransportIssue } from "@/components/assistant/transport-status";
import { AssistantRuntimeTransportHost } from "@/components/assistant/runtime/assistant-runtime-transport-host";
import { useRuntimeCancellation } from "@/components/assistant/runtime/use-runtime-cancellation";
import { useRuntimeDiagnostics } from "@/components/assistant/runtime/use-runtime-diagnostics";
import { useBusinessResume } from "@/components/assistant/runtime/use-business-resume";
import { useCancellationConfirmation } from "@/components/assistant/runtime/use-cancellation-confirmation";
import { useRuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import { useBackendTransitions } from "@/components/assistant/runtime/use-backend-transitions";
import type {
  AssistantRuntimeProps,
  ComposerRestore,
  RuntimeControls,
  RuntimeSessionContext,
} from "@/components/assistant/runtime/runtime-types";
import { createAssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readStoredToolGroupSelection, writeStoredToolGroupSelection } from "@/lib/tool-group-selection-storage";
import { newTraceId } from "@/lib/trace";
import { getToolGroups, toolNamesForGroups, type ToolGroupCatalog } from "@/lib/api/tools";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { queryKeys } from "@/lib/query/query-keys";
import {
  getBackendRuntimeSnapshot,
  subscribeBackendRuntime,
} from "@/src/runtime-config";

const EMPTY_TOOL_GROUPS: ToolGroupCatalog[] = [];

type RuntimeSessionProps = AssistantRuntimeProps & {
  setIssue: (issue: TransportIssue | null) => void;
  /** 向外层注册「重新同步」：外层的 TransportStatus 按钮经此触发会话内原地对账。 */
  registerResync: (resync: (() => void) | null) => void;
};

/** 组装后端 Transport runtime、生命周期协调器和独立的对话渲染宿主。 */
export const AssistantRuntimeSession = memo(function AssistantRuntimeSession({
  taskId,
  workspaceId,
  workspaceRoot,
  initialState,
  setIssue,
  registerResync,
  initialMessage,
  initialAttachments,
  initialDisabledToolGroups,
  initialBanTools,
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
    available: backendRuntimeAvailable,
  } = backendRuntime;
  const backendRef = useRef(backendRuntime);
  backendRef.current = backendRuntime;
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
  const [selectedToolGroups, setSelectedToolGroups] = useState<string[]>(() => (
    readStoredToolGroupSelection(taskId) ?? initialDisabledToolGroups ?? []
  ));
  // 后端不可用时不发请求；后端恢复 / 重启后的重新拉取由全局 query 同步统一失效。
  const toolGroupsQuery = useQuery({
    queryKey: queryKeys.toolGroups(),
    queryFn: ({ signal }) => getToolGroups({ signal }),
    enabled: backendRuntimeAvailable,
  });
  const toolGroups = toolGroupsQuery.data?.groups ?? EMPTY_TOOL_GROUPS;
  const toolGroupsLoading = toolGroupsQuery.isPending;
  const toolGroupsError = toolGroupsQuery.isError
    ? userErrorMessage(toolGroupsQuery.error, "工具分组加载失败")
    : null;
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

  const selectedBanTools = useMemo(() => {
    if (toolGroupsLoading || toolGroupsError) return initialBanTools ?? [];
    return toolNamesForGroups(selectedToolGroups, toolGroups);
  }, [initialBanTools, selectedToolGroups, toolGroups, toolGroupsError, toolGroupsLoading]);

  const registerRuntimeControls = useCallback((controls: RuntimeControls | null) => {
    runtimeControlsRef.current = controls;
  }, []);
  const handleSelectedToolGroupsChange = useCallback((groups: string[]) => {
    setSelectedToolGroups(groups);
    writeStoredToolGroupSelection(taskId, groups);
  }, [taskId]);
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
    backendRef,
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
  // 后端变化的唯一处理点，顺序即语义：先放弃依赖旧实例的业务续跑与取消轮询，
  // 再由 recovery 以 canonical snapshot 原地对账（运行中则续订，已被收敛则导入终态）。
  useBackendTransitions(backendRuntime, {
    onUnavailable: () => {
      businessResume.cancelBusinessResume();
      cancellationConfirmation.suspendForBackend();
      recovery.suspend();
    },
    onRestored: () => {
      businessResume.cancelBusinessResume();
      cancellationConfirmation.resumeAfterBackendRestored();
      recovery.resync("backend_restored", { message: "本机后端已恢复，正在同步当前对话…", canResync: true });
    },
  }, { taskId, traceId });

  useEffect(() => {
    registerResync(() => recovery.resync("user_resync", null));
    return () => registerResync(null);
  }, [recovery.resync, registerResync]);

  const commitTransportState = useCallback((state: typeof sessionInitialState) => {
    latestStateRef.current = state;
    cancellation.onStateCommitted(state);
  }, [cancellation.onStateCommitted, sessionInitialState]);
  return (
    <AssistantRuntimeTransportHost
      context={context}
      recovery={recovery}
      commitTransportState={commitTransportState}
      selectedBanTools={selectedBanTools}
      registerRuntimeControls={registerRuntimeControls}
      registerComposerRestore={registerComposerRestore}
      initialMessageSentRef={initialMessageSentRef}
      toolGroups={toolGroups}
      selectedToolGroups={selectedToolGroups}
      onSelectedToolGroupsChange={handleSelectedToolGroupsChange}
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
