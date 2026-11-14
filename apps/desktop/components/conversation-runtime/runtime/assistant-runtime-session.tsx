import { memo, useMemo } from "react";

import { AssistantRuntimeTransportHost } from "@/components/conversation-runtime/runtime/assistant-runtime-transport-host";
import { useRuntimeCancellation } from "@/components/conversation-runtime/runtime/use-runtime-cancellation";
import { useRuntimeDiagnostics } from "@/components/conversation-runtime/runtime/use-runtime-diagnostics";
import { useBusinessResume } from "@/components/conversation-runtime/runtime/use-business-resume";
import type {
  AssistantRuntimeProps,
  RuntimeSessionContext,
} from "@/components/conversation-runtime/runtime/runtime-types";

/**
 * 组装会话内的用例（诊断、业务续跑、停止状态）与独立的对话渲染宿主。
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
      forkingMessageId={forkingMessageId}
      onForkMessage={onForkMessage}
      onResumeBusiness={businessResume.resumeBusinessRun}
      cancelling={cancelling}
      performanceProbe={performanceProbe}
    />
  );
});
