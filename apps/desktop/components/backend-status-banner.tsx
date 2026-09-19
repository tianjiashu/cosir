"use client";

import { useCallback, useState, useSyncExternalStore } from "react";
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

  if (!status || status.state === "ready") return null;

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

  return (
    <div className="bg-destructive text-destructive-foreground fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-3 px-4 py-2 text-sm shadow-md">
      <span>
        {status.state === "failed"
          ? status.message
          : status.state === "stopped"
            ? "本地 Agent 后端未运行"
            : "本地 Agent 后端正在启动…"}
      </span>
      {(status.state === "failed" || status.state === "stopped") && (
        <button type="button" className="underline" onClick={() => void retry()} disabled={retrying}>
          {retrying ? "重试中…" : "重试"}
        </button>
      )}
    </div>
  );
}
