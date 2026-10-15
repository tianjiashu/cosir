"use client";

import { useSyncExternalStore } from "react";

import { useBackendRestart } from "@/components/backend-restart-button";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { getBackendStatusSnapshot, subscribeBackendStatus } from "@/src/runtime-config";

/**
 * 首次就绪之后，后端所有非就绪状态的唯一呈现位置。
 *
 * 启动门禁（App 的启动页 / BootFailure）只负责首次就绪之前；之后界面树始终保持
 * 挂载，由本横幅提示 starting / failed / stopped，任务页与草稿不随之卸载。
 * - starting：supervisor 正在启动或自动崩溃恢复，无需用户操作。
 * - failed / stopped：自动恢复已耗尽或进程未运行，只提供显式「重启后端」；
 *   此时没有运行中的任务可中断，因此不再二次确认。
 * runtime monitor 每次轮询都会重新发布状态，偶发的状态读取失败在下一次成功轮询后自动消失。
 */
export function BackendStatusBanner() {
  const status = useSyncExternalStore(
    subscribeBackendStatus,
    getBackendStatusSnapshot,
    getBackendStatusSnapshot,
  );
  const { restarting, restart } = useBackendRestart("status_banner");

  if (!status || status.state === "ready") return null;

  const unavailable = status.state === "failed" || status.state === "stopped";

  return (
    // 外层不透明底色承托内层半透明语义色，避免浮层透出下方内容。
    <div className="bg-background fixed inset-x-0 top-0 z-50 shadow-md">
      <div
        role={unavailable ? "alert" : "status"}
        className={cn(
          "flex items-center justify-center gap-3 border-b px-4 py-2 text-sm",
          unavailable ? "border-destructive/30 bg-destructive/10 text-destructive" : "border-info/30 bg-info/10 text-info",
        )}
      >
        {!unavailable && <Spinner label={null} />}
        <span>
          {status.state === "failed"
            ? status.message
            : status.state === "stopped"
              ? "本地 Agent 后端未运行"
              : "本地 Agent 后端正在启动，已打开的对话会在就绪后自动同步…"}
        </span>
        {unavailable && (
          <Button type="button" variant="outline" size="sm" onClick={() => void restart()} disabled={restarting}>
            {restarting ? "重启中…" : "重启后端"}
          </Button>
        )}
      </div>
    </div>
  );
}
