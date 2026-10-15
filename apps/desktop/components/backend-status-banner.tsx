"use client";

import { useCallback, useState, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import {
  getBackendStatusSnapshot,
  restartBackendRuntime,
  subscribeBackendStatus,
} from "@/src/runtime-config";

export function BackendStatusBanner() {
  const status = useSyncExternalStore(
    subscribeBackendStatus,
    getBackendStatusSnapshot,
    getBackendStatusSnapshot,
  );
  const [retrying, setRetrying] = useState(false);

  const retry = useCallback(async () => {
    setRetrying(true);
    try {
      await restartBackendRuntime();
    } catch {
      // The controller publishes the failed/stopped status for the next render.
    } finally {
      setRetrying(false);
    }
  }, []);

  if (!status || status.state === "ready") return null;

  // 启动中是正常过渡态，用 info 语气；只有失败 / 已停止才用 destructive。
  const unavailable = status.state === "failed" || status.state === "stopped";

  return (
    // 外层不透明底色承托内层半透明语义色，避免浮层透出下方内容。
    <div className="bg-background fixed inset-x-0 top-0 z-50 shadow-md">
      <div
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
              : "本地 Agent 后端正在启动…"}
        </span>
        {unavailable && (
          <Button type="button" variant="link" size="sm" className="h-auto px-0 text-current underline" onClick={() => void retry()} disabled={retrying}>
            {retrying ? "重试中…" : "重试"}
          </Button>
        )}
      </div>
    </div>
  );
}
