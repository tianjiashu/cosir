"use client";

import { useState } from "react";
import { Loader2Icon, RotateCwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { canRestartBackendRuntime, restartBackendRuntime } from "@/src/runtime-config";
import { frontendLog } from "@/lib/logging/frontend-log";

export function BackendRestartButton() {
  const [restarting, setRestarting] = useState(false);

  if (!canRestartBackendRuntime()) return null;

  const restart = async () => {
    if (restarting) return;
    setRestarting(true);
    void frontendLog("INFO", "backend_restart_requested", "用户请求重启本地后端");
    try {
      await restartBackendRuntime();
      void frontendLog("INFO", "backend_restart_completed", "本地后端重启完成");
    } catch (error) {
      void frontendLog("ERROR", "backend_restart_failed", "本地后端重启失败", { error });
    } finally {
      setRestarting(false);
    }
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      onClick={() => void restart()}
      disabled={restarting}
      aria-label={restarting ? "正在重启后端" : "重启后端"}
      title="重启本地后端；运行中的任务会中断，可在对话中恢复"
    >
      {restarting
        ? <Loader2Icon className="animate-spin" />
        : <RotateCwIcon />}
      {restarting ? "重启中…" : "重启后端"}
    </Button>
  );
}
