import { memo, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AssistantRuntimeTransportHost } from "@/components/conversation-runtime/runtime/assistant-runtime-transport-host";
import { useRuntimeCancellation } from "@/components/conversation-runtime/runtime/use-runtime-cancellation";
import { useRuntimeDiagnostics } from "@/components/conversation-runtime/runtime/use-runtime-diagnostics";
import { useBusinessResume } from "@/components/conversation-runtime/runtime/use-business-resume";
import type {
  AssistantRuntimeProps,
  RuntimeSessionContext,
} from "@/components/conversation-runtime/runtime/runtime-types";
import type { ToolGroupCatalog } from "@/lib/api/tool-groups";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";
import { toolGroupsQueryOptions } from "@/lib/query/query-options";

const EMPTY_TOOL_GROUPS: ToolGroupCatalog[] = [];

/**
 * 组装会话内的用例（诊断、业务续跑、停止状态、工具组）与独立的对话渲染宿主。
 *
 * 订阅、重连与后端实例变化都归外壳持有的连接，这里不再处理；挂载时连接已收到首帧，当前状态一律读 store。
 */
export const AssistantRuntimeSession = memo(function AssistantRuntimeSession({
  sessionId,
  workspaceId,
  workspaceRoot,
  connection,
  store,
  traceId,
  performanceProbe,
  forkingMessageId,
  onForkMessage,
}: AssistantRuntimeProps) {
  const { available: backendRuntimeAvailable } = useBackendRuntime();
  // 默认沿用最近一次 Run 冻结的工具组（后端事实，换设备 / 清存储都一致）；用户改动后到发送前
  // 只是组件状态，不另存本地。发送时直接传组名，不依赖下面的工具组目录是否已加载。
  const [selectedToolGroups, setSelectedToolGroups] = useState<string[]>(() => {
    const state = store.getSnapshot().state;
    return (state.activity?.launch ?? state.fork?.initialLaunch)?.disabledToolGroups ?? [];
  });
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

  const context = useMemo<RuntimeSessionContext>(() => ({
    sessionId,
    workspaceId,
    workspaceRoot,
    traceId,
    performanceProbe,
    connection,
    store,
  }), [connection, performanceProbe, store, sessionId, traceId, workspaceId, workspaceRoot]);

  useRuntimeDiagnostics(context);
  const businessResume = useBusinessResume(context);
  const cancelling = useRuntimeCancellation(context);
  return (
    <AssistantRuntimeTransportHost
      context={context}
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
