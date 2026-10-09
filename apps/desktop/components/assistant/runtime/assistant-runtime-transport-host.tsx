import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { memo, useCallback, useState } from "react";

import { AssistantThreadSurface } from "@/components/assistant/runtime/assistant-thread-surface";
import { useRuntimeTransport } from "@/components/assistant/runtime/use-runtime-transport";
import type { RuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import type { AssistantRuntimeProps, RuntimeSessionContext, RuntimeControls, ComposerRestore } from "@/components/assistant/runtime/runtime-types";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { TransportState } from "@/lib/assistant/contract";
import type { ToolGroupCatalog } from "@/lib/api/tools";
import type { MutableRefObject } from "react";

type AssistantRuntimeTransportHostProps = {
  context: RuntimeSessionContext;
  recovery: RuntimeRecovery;
  commitTransportState: (state: TransportState) => void;
  selectedBanTools: readonly string[];
  registerRuntimeControls: (controls: RuntimeControls | null) => void;
  registerComposerRestore: (restore: ComposerRestore) => void;
  initialMessageSentRef: MutableRefObject<boolean>;
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange: (groups: string[]) => void;
  toolGroupsLoading: boolean;
  toolGroupsError: string | null;
  initialMessage?: string;
  initialAttachments?: AssistantRuntimeProps["initialAttachments"];
  forkAvailable?: boolean;
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onRunStateChange?: (isRunning: boolean) => void;
  onResumeBusiness: () => Promise<void>;
  onInitialMessageError: (message: string) => void;
  onCancelRequested: (runId: number) => void;
  onCancelResult: (runId: number, accepted: boolean) => void;
  cancellingRunId: number | null;
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
  commitTransportState,
  selectedBanTools,
  registerRuntimeControls,
  registerComposerRestore,
  initialMessageSentRef,
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading,
  toolGroupsError,
  initialMessage,
  initialAttachments,
  forkAvailable,
  forkingRunId,
  onForkRun,
  onRunStateChange,
  onResumeBusiness,
  onInitialMessageError,
  onCancelRequested,
  onCancelResult,
  cancellingRunId,
  performanceProbe,
}: AssistantRuntimeTransportHostProps) {
  const [configurationProposalMode, setConfigurationProposalMode] = useState<"agent" | "agent-team" | null>(null);
  const consumeProposalMode = useCallback(() => setConfigurationProposalMode(null), []);
  const runtime = useRuntimeTransport(
    context,
    recovery,
    commitTransportState,
    selectedBanTools,
    configurationProposalMode,
    consumeProposalMode,
  );
  const resumeOnMount = context.initialState.runs.some((run) =>
    run.runId === context.initialState.current_run_id
      && (run.status === "pending" || run.status === "running"),
  );

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <AssistantThreadSurface
        taskId={context.taskId}
        workspaceId={context.workspaceId}
        workspaceRoot={context.workspaceRoot}
        initialMessage={initialMessage}
        initialAttachments={initialAttachments}
        forkAvailable={forkAvailable}
        forkingRunId={forkingRunId}
        onForkRun={onForkRun}
        onRunStateChange={onRunStateChange}
        initialState={context.initialState}
        backendAvailable={context.backendRuntimeAvailable}
        backendGeneration={context.backendRuntimeGeneration}
        resumeOnMount={resumeOnMount}
        attachTransportRef={context.attachTransportRef}
        registerRuntimeControls={registerRuntimeControls}
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
        cancellingRunId={cancellingRunId}
        performanceProbe={performanceProbe}
        configurationProposalMode={configurationProposalMode}
        onConfigurationProposalModeChange={setConfigurationProposalMode}
      />
    </AssistantRuntimeProvider>
  );
});
