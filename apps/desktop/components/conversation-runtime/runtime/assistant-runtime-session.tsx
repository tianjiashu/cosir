import type { ToolCalls } from "@/lib/assistant/tool-call-index";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { type TransportIssue } from "@/components/conversation-runtime/transport-status";
import { AssistantRuntimeTransportHost } from "@/components/conversation-runtime/runtime/assistant-runtime-transport-host";
import { useRuntimeCancellation } from "@/components/conversation-runtime/runtime/use-runtime-cancellation";
import { useRuntimeDiagnostics } from "@/components/conversation-runtime/runtime/use-runtime-diagnostics";
import { useBusinessResume } from "@/components/conversation-runtime/runtime/use-business-resume";
import { useSubscriptionRecovery } from "@/components/conversation-runtime/runtime/use-runtime-recovery";
import { useBackendTransitions } from "@/components/conversation-runtime/runtime/use-backend-transitions";
import type {
  AssistantRuntimeProps,
  RuntimeSessionContext,
} from "@/components/conversation-runtime/runtime/runtime-types";
import { createAssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useBackgroundTasksStore } from "@/lib/background-tasks/background-tasks-store";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { newTraceId } from "@/lib/logging/trace-id";
import type { ToolGroupCatalog } from "@/lib/api/tool-groups";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";
import { toolGroupsQueryOptions } from "@/lib/query/query-options";

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
  forkingMessageId,
  onForkMessage,
  onTaskStateChanged,
}: RuntimeSessionProps) {
  const backendRuntime = useBackendRuntime();
  const { available: backendRuntimeAvailable } = backendRuntime;
  const backendRef = useRef(backendRuntime);
  backendRef.current = backendRuntime;
  const [traceId] = useState(() => newTraceId());
  const performanceProbe = useMemo(() => {
    if (!import.meta.env.DEV) return null;
    return createAssistantPerformanceProbe({
      onReport: (report) => {
        void frontendLog("INFO", "assistant_render_performance_report", "Assistant 开发期渲染性能报告已采集", {
          traceId,
          data: { taskId, ...report },
        });
      },
    });
  }, [taskId, traceId]);
  // 默认沿用最近一次 Run 冻结的工具组（后端事实，换设备 / 清存储都一致）；用户改动后到发送前
  // 只是组件状态，不另存本地。发送时直接传组名，不依赖下面的工具组目录是否已加载。
  const [selectedToolGroups, setSelectedToolGroups] = useState<string[]>(
    () => (initialState.activity?.launch ?? initialState.fork?.initialLaunch)?.disabledToolGroups ?? [],
  );
  // 后端不可用时不发请求；后端恢复 / 重启后的重新拉取由全局 query 同步统一失效。
  const toolGroupsQuery = useQuery({
    ...toolGroupsQueryOptions(),
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
  const onTaskStateChangedRef = useRef(onTaskStateChanged);
  onTaskStateChangedRef.current = onTaskStateChanged;

  const notifyTaskStateChanged = useCallback(() => {
    onTaskStateChangedRef.current?.();
  }, []);

  const context = useMemo<RuntimeSessionContext>(() => ({
    taskId,
    workspaceId,
    workspaceRoot,
    initialState: sessionInitialState,
    backendRef,
    traceId,
    performanceProbe,
    setIssue,
    onTaskStateChanged: notifyTaskStateChanged,
    latestStateRef,
    attachTransportRef,
  }), [
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
  const recoveryDeps = useMemo(() => ({
    taskId: context.taskId,
    traceId: context.traceId,
    attach: () => context.attachTransportRef.current,
    backend: () => context.backendRef.current,
    setIssue: context.setIssue,
  }), [context]);
  const recovery = useSubscriptionRecovery(recoveryDeps);
  const businessResume = useBusinessResume(context);
  const cancelling = useRuntimeCancellation(context);
  // 后端变化的唯一处理点：不可用时停掉重连计时器；恢复后 runtime 按新实例自行重开订阅（首帧即
  // 完整状态），这里只清零预算并提示。
  useBackendTransitions(backendRuntime, {
    onUnavailable: () => {
      recovery.resetBudget();
    },
    onRestored: () => {
      recovery.restored({ message: "本机后端已恢复，正在同步当前对话…", canResync: true });
    },
  }, { taskId, traceId });

  useEffect(() => {
    registerResync(() => recovery.resync(null));
    return () => registerResync(null);
  }, [recovery.resync, registerResync]);

  const commitTransportState = useCallback((state: typeof sessionInitialState, toolCalls: ToolCalls) => {
    const previousTitle = latestStateRef.current.title;
    latestStateRef.current = state;
    // 终端会话列表是 snapshot 投影；镜像给页头按钮与终端工作台（它们在 Assistant runtime 之外）。
    useTerminalWorkbenchStore.getState().syncSessions(taskId, state.terminal_sessions);
    useBackgroundTasksStore.getState().sync(taskId, { ...state, toolCalls });
    useHandoffStore.getState().sync(taskId, state.handoffs);
    // 标题事实在后端 tasks.title；snapshot 只负责通知打开中的页面，侧栏目录按既有刷新路径重读。
    if (state.title !== previousTitle) notifyTaskStateChanged();
  }, [notifyTaskStateChanged, taskId]);
  return (
    <AssistantRuntimeTransportHost
      context={context}
      recovery={recovery}
      commitTransportState={commitTransportState}
      toolGroups={toolGroups}
      selectedToolGroups={selectedToolGroups}
      onSelectedToolGroupsChange={setSelectedToolGroups}
      toolGroupsLoading={toolGroupsLoading}
      toolGroupsError={toolGroupsError}
      forkingMessageId={forkingMessageId}
      onForkMessage={onForkMessage}
      onResumeBusiness={businessResume.resumeBusinessRun}
      cancelling={cancelling}
      performanceProbe={performanceProbe}
    />
  );
});
