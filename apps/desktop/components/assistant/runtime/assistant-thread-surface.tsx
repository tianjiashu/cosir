import { memo } from "react";

import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import {
  ComposerRestoreBridge,
  RuntimeRenderDiagnostics,
} from "@/components/assistant/runtime/assistant-runtime-bridges";
import type {
  AssistantRuntimeProps,
  ComposerRestore,
} from "@/components/assistant/runtime/runtime-types";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { ToolGroupCatalog } from "@/lib/api/tools";

type AssistantThreadSurfaceProps = Pick<
  AssistantRuntimeProps,
  | "taskId"
  | "workspaceId"
  | "workspaceRoot"
  | "forkingMessageId"
  | "onForkMessage"
> & {
  registerComposerRestore: (restore: ComposerRestore) => void;
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange: (groups: string[]) => void;
  toolGroupsLoading: boolean;
  toolGroupsError: string | null;
  onResumeBusiness: () => Promise<void>;
  onCancelRequested: () => void;
  onCancelResult: (accepted: boolean) => void;
  cancelling: boolean;
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
  forkingMessageId,
  onForkMessage,
  registerComposerRestore,
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  toolGroupsLoading,
  toolGroupsError,
  onResumeBusiness,
  onCancelRequested,
  onCancelResult,
  cancelling,
  performanceProbe,
}: AssistantThreadSurfaceProps) {
  return (
    <>
      <ComposerRestoreBridge register={registerComposerRestore} />
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
          forkingMessageId={forkingMessageId}
          onForkMessage={onForkMessage}
          onResumeBusiness={onResumeBusiness}
          onCancelRequested={onCancelRequested}
          onCancelResult={onCancelResult}
          cancelling={cancelling}
          performanceProbe={performanceProbe}
        />
      </div>
    </>
  );
});
