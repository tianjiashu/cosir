"use client";

import { useEffect, useState } from "react";

import { WorkspaceShell } from "@/components/workspace-shell";
import { getTask } from "@/lib/api/workspaces";

export function TaskPage({ taskId }: { taskId: number }) {
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void getTask(taskId)
      .then(() => {
        if (active) setState("ready");
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "任务加载失败，请重试");
        setState("error");
      });
    return () => {
      active = false;
    };
  }, [taskId]);

  if (state === "loading") return <div className="flex h-dvh items-center justify-center text-sm">正在加载任务…</div>;
  if (state === "error") return <div className="flex h-dvh items-center justify-center text-sm text-destructive">{error}</div>;
  return <WorkspaceShell initialTaskId={taskId} />;
}
