"use client";

import { useEffect, useRef, useState } from "react";

import { Assistant } from "@/app/assistant";
import { getTask, type WorkspaceTask } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";

type TaskSessionProps = {
  taskId: number;
  initialTask?: WorkspaceTask;
  initialMessage?: string;
  forkAvailable?: boolean;
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onTaskLoaded?: (task: WorkspaceTask) => void;
  onTaskStateChanged?: () => void;
  onRunStateChange?: (isRunning: boolean) => void;
};

/**
 * 任务作用域的 UI session。
 *
 * WorkspaceShell 由路由稳定持有，TaskSession 才随 taskId 变化。这样任务历史
 * 和 Assistant Transport 可以按任务重新挂载，而 workspace/task 侧栏不会被卸载。
 */
export function TaskPage({
  taskId,
  initialTask,
  initialMessage,
  forkAvailable,
  forkingRunId,
  onForkRun,
  onTaskLoaded,
  onTaskStateChanged,
  onRunStateChange,
}: TaskSessionProps) {
  const [loadedTask, setLoadedTask] = useState<WorkspaceTask | null>(
    initialTask?.task_id === taskId ? initialTask : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const requestGenerationRef = useRef(0);

  useEffect(() => {
    const requestGeneration = ++requestGenerationRef.current;
    const taskFromSidebar = initialTask?.task_id === taskId ? initialTask : null;
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
        setError(cause instanceof Error ? cause.message : "任务加载失败，请重试");
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
  }, [initialTask, onTaskLoaded, retryToken, taskId]);

  if (error) return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-sm">
      <p className="text-destructive">{error}</p>
      <button type="button" className="text-muted-foreground underline underline-offset-4" onClick={() => setRetryToken((value) => value + 1)}>重试</button>
    </div>
  );
  if (!loadedTask || loadedTask.task_id !== taskId) return <div className="flex h-full items-center justify-center text-sm">正在加载任务工作区…</div>;

  return (
    <Assistant
      taskId={taskId}
      workspaceId={loadedTask.workspace_id}
      initialMessage={initialMessage}
      forkAvailable={forkAvailable ?? loadedTask.fork_available}
      forkingRunId={forkingRunId}
      onForkRun={onForkRun}
      onTaskStateChanged={onTaskStateChanged}
      onRunStateChange={onRunStateChange}
    />
  );
}
