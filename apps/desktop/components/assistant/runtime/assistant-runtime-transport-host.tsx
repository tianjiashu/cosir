import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { memo } from "react";

import { AssistantThreadSurface } from "@/components/assistant/runtime/assistant-thread-surface";
import { useRuntimeTransport } from "@/components/assistant/runtime/use-runtime-transport";
import type { RuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import type { RuntimeCancellation } from "@/components/assistant/runtime/use-runtime-cancellation";
import type { AssistantRuntimeProps, RuntimeSessionContext, ComposerRestore } from "@/components/assistant/runtime/runtime-types";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { TransportState } from "@/lib/assistant/contract";
import type { ToolGroupCatalog } from "@/lib/api/tools";
import type { MutableRefObject } from "react";

type AssistantRuntimeTransportHostProps = {
  context: RuntimeSessionContext;
  recovery: RuntimeRecovery;
  cancellation: RuntimeCancellation;
  commitTransportState: (state: TransportState) => void;
  selectedBanTools: readonly string[];
  registerComposerRestore: (restore: ComposerRestore) => void;
  initialMessageSentRef: MutableRefObject<boolean>;
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange: (groups: string[]) => void;
  toolGroupsLoading: boolean;
  toolGroupsError: string | null;
  initialMessage?: string;
  initialAttachments?: AssistantRuntimeProps["initialAttachments"];
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  onResumeBusiness: () => Promise<void>;
  onInitialMessageError: (message: string) => void;
  onCancelRequested: () => void;
  onCancelResult: (accepted: boolean) => void;
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
  recovery,
  cancellation,
  commitTransportState,
  selectedBanTools,
  registerComposerRestore,
  initialMessageSentRef,
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading,
  toolGroupsError,
  initialMessage,
  initialAttachments,
  forkingMessageId,
  onForkMessage,
  onResumeBusiness,
  onInitialMessageError,
  onCancelRequested,
  onCancelResult,
  cancelling,
  performanceProbe,
}: AssistantRuntimeTransportHostProps) {
  const runtime = useRuntimeTransport(
    context,
    recovery,
    cancellation,
    commitTransportState,
    selectedBanTools,
  );

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <AssistantThreadSurface
        taskId={context.taskId}
        workspaceId={context.workspaceId}
        workspaceRoot={context.workspaceRoot}
        initialMessage={initialMessage}
        initialAttachments={initialAttachments}
        forkingMessageId={forkingMessageId}
        onForkMessage={onForkMessage}
        initialState={context.initialState}
        registerComposerRestore={registerComposerRestore}
        initialMessageSentRef={initialMessageSentRef}
        traceId={context.traceId}
        onInitialMessageError={onInitialMessageError}
        toolGroups={toolGroups}
        selectedToolGroups={selectedToolGroups}
        onSelectedToolGroupsChange={onSelectedToolGroupsChange}
        toolGroupsLoading={toolGroupsLoading}
        toolGroupsError={toolGroupsError}
        onResumeBusiness={onResumeBusiness}
        onCancelRequested={onCancelRequested}
        onCancelResult={onCancelResult}
        cancelling={cancelling}
        performanceProbe={performanceProbe}
      />
    </AssistantRuntimeProvider>
  );
});
