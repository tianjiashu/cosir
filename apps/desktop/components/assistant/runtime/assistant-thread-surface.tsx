import { memo, type MutableRefObject } from "react";

import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import {
  ComposerRestoreBridge,
  InitialMessageBridge,
  RuntimeControlBridge,
  RuntimeRenderDiagnostics,
  TaskStateBridge,
} from "@/components/assistant/runtime/assistant-runtime-bridges";
import type {
  AssistantRuntimeProps,
  ComposerRestore,
  RuntimeControls,
} from "@/components/assistant/runtime/runtime-types";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { TransportState } from "@/lib/assistant/contract";
import type { ToolGroupCatalog } from "@/lib/api/tools";

type AssistantThreadSurfaceProps = Pick<
  AssistantRuntimeProps,
  | "taskId"
  | "workspaceId"
  | "workspaceRoot"
  | "initialMessage"
  | "initialAttachments"
  | "forkAvailable"
  | "forkingRunId"
  | "onForkRun"
  | "onRunStateChange"
> & {
  initialState: TransportState;
  backendAvailable: boolean;
  backendGeneration: number;
  resumeOnMount: boolean;
  attachTransportRef: MutableRefObject<(() => Promise<void>) | null>;
  registerRuntimeControls: (controls: RuntimeControls | null) => void;
  registerComposerRestore: (restore: ComposerRestore) => void;
  initialMessageSentRef: MutableRefObject<boolean>;
  traceId: string;
  onInitialMessageError: (message: string) => void;
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange: (groups: string[]) => void;
  toolGroupsLoading: boolean;
  toolGroupsError: string | null;
  onResumeBusiness: () => Promise<void>;
  onCancelRequested: (runId: number) => void;
  onCancelResult: (runId: number, accepted: boolean) => void;
  cancellingRunId: number | null;
  performanceProbe: AssistantPerformanceProbe | null;
};

/**
 * Assistant UI 的稳定渲染表面。
 *
 * 该组件只接收会话级输入，并把 Thread、Composer 与消息列表留在独立的 memo 边界内。
 * Transport host 可以因帧快照变化重新执行，但不会因此重新执行这棵表面的函数组件。
 * 本组件不创建 transport、不写入后端，也不改变 Assistant Transport wire schema。
 */
export const AssistantThreadSurface = memo(function AssistantThreadSurface({
  taskId,
  workspaceId,
  workspaceRoot,
  initialMessage,
  initialAttachments,
  forkAvailable,
  forkingRunId,
  onForkRun,
  onRunStateChange,
  initialState,
  backendAvailable,
  backendGeneration,
  resumeOnMount,
  attachTransportRef,
  registerRuntimeControls,
  registerComposerRestore,
  initialMessageSentRef,
  traceId,
  onInitialMessageError,
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading,
  toolGroupsError,
  onResumeBusiness,
  onCancelRequested,
  onCancelResult,
  cancellingRunId,
  performanceProbe,
}: AssistantThreadSurfaceProps) {
  return (
    <>
      <RuntimeControlBridge
        register={registerRuntimeControls}
        backendAvailable={backendAvailable}
        backendGeneration={backendGeneration}
        resumeOnMount={resumeOnMount}
        attachTransportRef={attachTransportRef}
      />
      <ComposerRestoreBridge register={registerComposerRestore} />
      <TaskStateBridge onRunStateChange={onRunStateChange} />
      <RuntimeRenderDiagnostics taskId={taskId} />
      <div className="flex h-full min-h-0 flex-col">
        <Thread
          taskId={taskId}
          workspaceId={workspaceId}
          toolGroups={toolGroups}
          selectedToolGroups={selectedToolGroups}
          onSelectedToolGroupsChange={onSelectedToolGroupsChange}
          toolGroupsLoading={toolGroupsLoading}
          toolGroupsError={toolGroupsError}
          workspaceRoot={workspaceRoot}
          forkAvailable={forkAvailable}
          forkingRunId={forkingRunId}
          onForkRun={onForkRun}
          onResumeBusiness={onResumeBusiness}
          onCancelRequested={onCancelRequested}
          onCancelResult={onCancelResult}
          cancellingRunId={cancellingRunId}
          performanceProbe={performanceProbe}
        />
      </div>
      <InitialMessageBridge
        text={initialMessage}
        attachments={initialAttachments}
        sentRef={initialMessageSentRef}
        initialState={initialState}
        taskId={taskId}
        traceId={traceId}
        onError={onInitialMessageError}
      />
    </>
  );
});
