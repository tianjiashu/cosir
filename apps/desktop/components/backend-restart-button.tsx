"use client";

import { useCallback, useState } from "react";
import { RotateCwIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { Spinner } from "@/components/ui/spinner";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { canRestartBackendRuntime, restartBackendRuntime } from "@/src/runtime-config";

/**
 * 用户显式「重启后端」的唯一入口：顶栏开发期按钮与后端状态横幅共用。
 *
 * 重启由 Tauri supervisor 执行并重置其崩溃恢复预算；失败状态由
 * runtime-config 发布给横幅，这里只负责 busy 态与结构化日志，不另行展示错误。
 * `source` 只用于区分日志来源。
 */
export function useBackendRestart(source: "header" | "status_banner") {
  const [restarting, setRestarting] = useState(false);

  const restart = useCallback(async () => {
    setRestarting(true);
    void frontendLog("INFO", "backend_restart_requested", "用户请求重启本地后端", { data: { source } });
    try {
      await restartBackendRuntime();
      void frontendLog("INFO", "backend_restart_completed", "本地后端重启完成", { data: { source } });
    } catch (error) {
      void frontendLog("ERROR", "backend_restart_failed", "本地后端重启失败", { data: { source }, error });
    } finally {
      setRestarting(false);
    }
  }, [source]);

  return { restarting, restart };
}

/**
 * 开发期顶栏的重启按钮，与顶栏其余操作一样是图标按钮。后端就绪时重启会中断所有运行中的
 * 任务，因此必须二次确认；中断的 Run 由后端收敛为 cancelled，用户可在对话中继续。
 */
export function BackendRestartButton() {
  const { restarting, restart } = useBackendRestart("header");

  if (!canRestartBackendRuntime()) return null;

  const confirmAndRestart = () => {
    void requestConfirmation({
      title: "重启本地后端？",
      description: "后端会停止并重新启动，期间无法发送消息。",
      warning: "所有正在运行的任务都会被中断；重启后可在对话中继续运行。",
      confirmLabel: "重启后端",
      busyLabel: "重启中…",
      icon: "restart",
      // restart 自行记录失败日志，横幅呈现失败状态，因此对话框总是关闭。
      action: restart,
    });
  };

  return (
    <TooltipIconButton
      tooltip={restarting ? "重启中…" : "重启后端"}
      className="size-7"
      onClick={confirmAndRestart}
      disabled={restarting}
      aria-label={restarting ? "正在重启后端" : "重启后端"}
    >
      {restarting ? <Spinner label={null} /> : <RotateCwIcon />}
    </TooltipIconButton>
  );
}
