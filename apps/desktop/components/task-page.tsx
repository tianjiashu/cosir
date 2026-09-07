"use client";

import { useEffect, useState } from "react";

import { WorkspaceShell } from "@/components/workspace-shell";
import { getTask, type WorkspaceTask } from "@/lib/api/workspaces";

export function TaskPage({ taskId, initialMessage }: { taskId: number; initialMessage?: string }) {
  const [loadedTask, setLoadedTask] = useState<{ taskId: number; task: WorkspaceTask } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    let active = true;
    setLoadedTask(null);
    setError(null);
    void getTask(taskId)
      .then((task) => {
        if (active) setLoadedTask({ taskId, task });
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "任务加载失败，请重试");
      });
    return () => {
      active = false;
    };
  }, [retryToken, taskId]);

  if (error) return (
    <div className="flex h-dvh flex-col items-center justify-center gap-3 text-sm">
      <p className="text-destructive">{error}</p>
      <button type="button" className="text-muted-foreground underline underline-offset-4" onClick={() => setRetryToken((value) => value + 1)}>重试</button>
    </div>
  );
  if (!loadedTask || loadedTask.taskId !== taskId) return <div className="flex h-dvh items-center justify-center text-sm">正在加载任务…</div>;
  return <WorkspaceShell initialTask={loadedTask.task} initialMessage={initialMessage} />;
}
