import { useCallback, useState } from "react";

import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { restartBackendRuntime } from "@/lib/backend/runtime-config";

type RestartSource = "status_banner" | "command_palette";

/**
 * 用户显式「重启后端」的唯一实现：故障横幅与命令面板（开发期）共用。
 *
 * 重启由宿主 supervisor 执行并重置其崩溃恢复预算；失败状态由 runtime-config 发布给横幅，这里只记结构化
 * 日志、不另行展示错误，所以从不抛出。`source` 只用于区分日志来源。
 */
async function restartBackend(source: RestartSource): Promise<void> {
  void frontendLog("INFO", "backend_restart_requested", "用户请求重启本地后端", { data: { source } });
  try {
    await restartBackendRuntime();
    void frontendLog("INFO", "backend_restart_completed", "本地后端重启完成", { data: { source } });
  } catch (error) {
    void frontendLog("ERROR", "backend_restart_failed", "本地后端重启失败", { data: { source }, error });
  }
}

/** 横幅用：带 busy 态的重启（横幅出现时后端已不可用，无需二次确认）。 */
export function useBackendRestart(source: RestartSource) {
  const [restarting, setRestarting] = useState(false);
  const restart = useCallback(async () => {
    setRestarting(true);
    try {
      await restartBackend(source);
    } finally {
      setRestarting(false);
    }
  }, [source]);
  return { restarting, restart };
}

/**
 * 主动重启（命令面板）：后端就绪时重启会中断所有运行中的会话，因此必须二次确认；中断的 Run 由后端收敛为
 * cancelled，用户可在会话中继续。是否提供这个入口由 `canRestartBackendRuntime` 决定（只在开发期）。
 */
export function confirmBackendRestart(source: RestartSource) {
  void requestConfirmation({
    title: "重启本地后端？",
    description: "后端会停止并重新启动，期间无法发送消息。",
    warning: "所有正在运行的会话都会被中断；重启后可在会话中继续运行。",
    confirmLabel: "重启后端",
    busyLabel: "重启中…",
    icon: "restart",
    // 失败由横幅呈现，因此对话框总是关闭。
    action: () => restartBackend(source),
  });
}
