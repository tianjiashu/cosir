import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { memo } from "react";

import { AssistantThreadSurface } from "@/components/conversation-runtime/runtime/assistant-thread-surface";
import { useRuntimeTransport } from "@/components/conversation-runtime/runtime/use-runtime-transport";
import type { RuntimeSessionContext } from "@/components/conversation-runtime/runtime/runtime-types";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { ToolGroupCatalog } from "@/lib/api/tool-groups";
import { TransportHistoryContext } from "@/lib/assistant/transport/transport-history-context";

type AssistantRuntimeTransportHostProps = {
  context: RuntimeSessionContext;
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange: (groups: string[]) => void;
  toolGroupsLoading: boolean;
  toolGroupsError: string | null;
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  onResumeBusiness: () => Promise<void>;
  cancelling: boolean;
  performanceProbe: AssistantPerformanceProbe | null;
};

/**
 * 独立持有 Assistant Transport runtime 的宿主。
 *
 * Transport frame store 的快照更新只会重新执行本组件；稳定的 Thread surface 由 memo
 * 边界承接。这里仅装配运行时和已有桥接，不新增请求、持久化事实或 wire 字段。
 */
export const AssistantRuntimeTransportHost = memo(function AssistantRuntimeTransportHost({
  context,
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading,
  toolGroupsError,
  forkingMessageId,
  onForkMessage,
  onResumeBusiness,
  cancelling,
  performanceProbe,
}: AssistantRuntimeTransportHostProps) {
  const { runtime, loadEarlierHistory } = useRuntimeTransport(context, selectedToolGroups);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TransportHistoryContext.Provider value={loadEarlierHistory}>
      <AssistantThreadSurface
        taskId={context.taskId}
        workspaceId={context.workspaceId}
        workspaceRoot={context.workspaceRoot}
        forkingMessageId={forkingMessageId}
        onForkMessage={onForkMessage}
        toolGroups={toolGroups}
        selectedToolGroups={selectedToolGroups}
        onSelectedToolGroupsChange={onSelectedToolGroupsChange}
        toolGroupsLoading={toolGroupsLoading}
        toolGroupsError={toolGroupsError}
        onResumeBusiness={onResumeBusiness}
        cancelling={cancelling}
        performanceProbe={performanceProbe}
      />
      </TransportHistoryContext.Provider>
    </AssistantRuntimeProvider>
  );
});
