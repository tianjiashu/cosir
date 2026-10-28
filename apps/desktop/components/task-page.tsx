"use client";

import { useEffect, useRef, useState } from "react";

import { Assistant } from "@/app/assistant";
import type { InitialConversationAttachment } from "@/components/new-conversation";
import { getTask, type WorkspaceTask } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { TerminalWorkbench } from "@/components/terminal/terminal-workbench";
import { useBackgroundTasksStore } from "@/lib/task-overview/background-tasks-store";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { IsolatedRegion } from "@/components/render-isolation/isolated-region";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { TaskWorkspaceLayout } from "@/components/task-workspace-layout";
import { ErrorState } from "@/components/ui/state-panel";
import { ThreadSkeleton } from "@/components/assistant/thread-skeleton";

type TaskSessionProps = {
  taskId: number;
  initialTask?: WorkspaceTask;
  initialMessage?: string;
  initialAttachments?: InitialConversationAttachment[];
  workspaceRoot?: string;
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  onTaskLoaded?: (task: WorkspaceTask) => void;
  onTaskStateChanged?: () => void;
};

/**
 * 任务作用域的 UI session。
 *
 * AppLayout 由路由稳定持有，TaskSession 才随 taskId 变化。这样任务历史
 * 和 Assistant Transport 可以按任务重新挂载，而 workspace/task 侧栏不会被卸载。
 */
export function TaskPage({
  taskId,
  initialTask,
  initialMessage,
  initialAttachments,
  workspaceRoot,
  forkingMessageId,
  onForkMessage,
  onTaskLoaded,
  onTaskStateChanged,
}: TaskSessionProps) {
  const initialTaskId = initialTask?.task_id ?? null;
  const [loadedTask, setLoadedTask] = useState<WorkspaceTask | null>(
    initialTaskId === taskId ? initialTask ?? null : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const requestGenerationRef = useRef(0);

  useEffect(() => {
    const requestGeneration = ++requestGenerationRef.current;
    const taskFromSidebar = initialTaskId === taskId ? initialTask ?? null : null;
    void frontendLog("DEBUG", "task_session_load_started", "Task session 开始解析任务身份", {
      data: {
        taskId,
        requestGeneration,
        source: taskFromSidebar ? "sidebar_cache" : "task_endpoint",
        retryToken,
      },
    });
    if (taskFromSidebar) {
      setLoadedTask(taskFromSidebar);
      setError(null);
      onTaskLoaded?.(taskFromSidebar);
      void frontendLog("DEBUG", "task_session_loaded_from_sidebar", "Task session 使用侧栏缓存任务", {
        data: { taskId, workspaceId: taskFromSidebar.workspace_id, requestGeneration },
      });
      return;
    }

    const controller = new AbortController();
    setLoadedTask(null);
    setError(null);
    void getTask(taskId, { signal: controller.signal })
      .then((task) => {
        if (controller.signal.aborted || requestGeneration !== requestGenerationRef.current) {
          void frontendLog("DEBUG", "task_session_response_discarded", "Task session 丢弃过期任务响应", {
            data: { taskId, requestGeneration, currentGeneration: requestGenerationRef.current, aborted: controller.signal.aborted },
          });
          return;
        }
        setLoadedTask(task);
        onTaskLoaded?.(task);
        void frontendLog("DEBUG", "task_session_loaded", "Task session 任务加载完成", {
          data: { taskId, workspaceId: task.workspace_id, requestGeneration },
        });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || requestGeneration !== requestGenerationRef.current) {
          void frontendLog("DEBUG", "task_session_error_discarded", "Task session 丢弃过期任务错误", {
            data: { taskId, requestGeneration, currentGeneration: requestGenerationRef.current, aborted: controller.signal.aborted },
          });
          return;
        }
        setError(userErrorMessage(cause, "任务加载失败，请重试"));
        void frontendLog("WARNING", "task_session_load_failed", "Task session 任务加载失败", {
          data: { taskId, requestGeneration },
          error: cause,
        });
      });
    return () => {
      controller.abort();
      void frontendLog("DEBUG", "task_session_load_aborted", "Task session 任务请求已 abort", {
        data: { taskId, requestGeneration },
      });
    };
  // 依赖 initialTaskId（原始 task_id）而非 initialTask 对象引用：父组件在 run 状态
  // 切换等无关更新时会重建 activeTask 对象引用，若依赖整对象会触发冗余重渲染链。
  }, [initialTaskId, onTaskLoaded, retryToken, taskId]);

  // 终端会话镜像只在本 Task 页存在期间有效：离开时清空，回来时等新 snapshot 再挂载窗格（见 store 说明）。
  useEffect(() => () => {
    useTerminalWorkbenchStore.getState().syncSessions(taskId, null);
    useBackgroundTasksStore.getState().sync(taskId, null);
  }, [taskId]);

  if (error) return (
    <div className="flex h-full items-center justify-center p-6">
      <ErrorState className="w-full max-w-md" message={error} onRetry={() => setRetryToken((value) => value + 1)} />
    </div>
  );
  // 与 Assistant 拉取首屏快照阶段共用同一骨架，两段加载在视觉上连续。
  if (!loadedTask || loadedTask.task_id !== taskId) return <ThreadSkeleton />;

  return (
    <TaskWorkspaceLayout bottomPanel={(
      // 终端面板崩溃只降级面板本身，不能连带卸载正在运行的 Assistant 会话。
      <IsolatedRegion
        region="terminal-panel"
        label="终端面板"
        resetKey={`terminal:${taskId}`}
        metadata={{ taskId }}
        fallbackClassName="dock-panel bg-background mx-2 mb-2 shrink-0 p-3"
        layout="row"
        // 先关闭再重置边界：重新挂载的 Host 因面板已关闭而不渲染，不会再次触发同一崩溃。
        escape={{ label: "收起终端", onClick: (reset) => { useTerminalWorkbenchStore.getState().setOpen(taskId, false); reset(); } }}
      >
        <TerminalWorkbench taskId={taskId} />
      </IsolatedRegion>
    )}>
      <div className="h-full min-h-0 min-w-0 w-full">
        <Assistant
          taskId={taskId}
          workspaceId={loadedTask.workspace_id}
          workspaceRoot={workspaceRoot}
          initialMessage={initialMessage}
          initialAttachments={initialAttachments}
          forkingMessageId={forkingMessageId}
          onForkMessage={onForkMessage}
          onTaskStateChanged={onTaskStateChanged}
        />
      </div>
    </TaskWorkspaceLayout>
  );
}
