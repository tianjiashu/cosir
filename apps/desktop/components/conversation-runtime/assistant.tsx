import { ErrorState } from "@/components/ui/state-panel";
import { useAssistantInitialState } from "@/components/conversation-runtime/use-assistant-initial-state";
import { AssistantRuntime } from "@/components/conversation-runtime/assistant-runtime";
import { ThreadSkeleton } from "@/components/conversation-runtime/thread-skeleton";
import { useState } from "react";

/**
 * 对话页入口：先拉取服务端首屏 state，就绪后再挂载 runtime。
 *
 * runtime 只在首次创建时捕获一次 `initialState`，之后的变化不再生效，所以必须先取后挂。
 * taskId 变化时 hook 先返回 null，旧 runtime 在新快照就绪前被卸载；这使 task 切换成为
 * 明确的页面生命周期，而不是依赖 key 重置活动请求。
 */
export const Assistant = ({
  taskId,
  workspaceId,
  workspaceRoot,
  forkingMessageId,
  onForkMessage,
  onTaskStateChanged,
}: {
  taskId: number;
  workspaceId: number;
  workspaceRoot?: string;
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  onTaskStateChanged?: () => void;
}) => {
  // AppLayout may finish its task-list refresh after the snapshot request.
  // Freeze the workspace identity for this task session so that a late parent
  // refresh cannot replace the Assistant UI transport while a run is active.
  const [sessionWorkspaceId] = useState(workspaceId);
  const { initialState, error, retry } = useAssistantInitialState(taskId);

  if (error) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center p-6">
        <ErrorState className="w-full max-w-md" message={error} onRetry={retry} />
      </div>
    );
  }

  if (!initialState) return <ThreadSkeleton />;

  return (
    <AssistantRuntime
      taskId={taskId}
      workspaceId={sessionWorkspaceId}
      workspaceRoot={workspaceRoot}
      initialState={initialState}
      forkingMessageId={forkingMessageId}
      onForkMessage={onForkMessage}
      onTaskStateChanged={onTaskStateChanged}
    />
  );
};
