import { memo, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";

import { type TransportIssue } from "@/components/assistant/transport-status";
import { AssistantRuntimeTransportHost } from "@/components/assistant/runtime/assistant-runtime-transport-host";
import { useRuntimeCancellation } from "@/components/assistant/runtime/use-runtime-cancellation";
import { useRuntimeDiagnostics } from "@/components/assistant/runtime/use-runtime-diagnostics";
import { useBusinessResume } from "@/components/assistant/runtime/use-business-resume";
import { useRuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import { useBackendTransitions } from "@/components/assistant/runtime/use-backend-transitions";
import type {
  AssistantRuntimeProps,
  ComposerRestore,
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
  forkingMessageId,
  onForkMessage,
  onTaskStateChanged,
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
  const attachTransportRef = useRef<(() => Promise<void>) | null>(null);
  const cancelRequestedRef = useRef(false);
  const composerRestoreRef = useRef<ComposerRestore | null>(null);
  const initialMessageSentRef = useRef(false);
  const onTaskStateChangedRef = useRef(onTaskStateChanged);
  onTaskStateChangedRef.current = onTaskStateChanged;

  const selectedBanTools = useMemo(() => {
    if (toolGroupsLoading || toolGroupsError) return initialBanTools ?? [];
    return toolNamesForGroups(selectedToolGroups, toolGroups);
  }, [initialBanTools, selectedToolGroups, toolGroups, toolGroupsError, toolGroupsLoading]);

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
    attachTransportRef,
    cancelRequestedRef,
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
  const recovery = useRuntimeRecovery(context);
  const businessResume = useBusinessResume(context);
  const cancellation = useRuntimeCancellation(context);
  // 后端变化的唯一处理点：不可用时停掉重连计时器；恢复后 runtime 按新实例自行重开订阅（首帧即
  // 完整状态），这里只清零预算并提示。
  useBackendTransitions(backendRuntime, {
    onUnavailable: () => {
      recovery.suspend();
    },
    onRestored: () => {
      recovery.restored({ message: "本机后端已恢复，正在同步当前对话…", canResync: true });
    },
  }, { taskId, traceId });

  useEffect(() => {
    registerResync(() => recovery.resync(null));
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
      cancellation={cancellation}
      commitTransportState={commitTransportState}
      selectedBanTools={selectedBanTools}
      registerComposerRestore={registerComposerRestore}
      initialMessageSentRef={initialMessageSentRef}
      toolGroups={toolGroups}
      selectedToolGroups={selectedToolGroups}
      onSelectedToolGroupsChange={handleSelectedToolGroupsChange}
      toolGroupsLoading={toolGroupsLoading}
      toolGroupsError={toolGroupsError}
      initialMessage={initialMessage}
      initialAttachments={initialAttachments}
      forkingMessageId={forkingMessageId}
      onForkMessage={onForkMessage}
      onResumeBusiness={businessResume.resumeBusinessRun}
      onInitialMessageError={handleInitialMessageError}
      onCancelRequested={cancellation.onRequested}
      onCancelResult={cancellation.onResult}
      cancelling={cancellation.cancelling}
      performanceProbe={performanceProbe}
    />
  );
});
