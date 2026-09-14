"use client";

import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  getBackendRuntimeSnapshot,
  setBackendBaseUrl,
  type BackendRuntimeConfig,
  type BackendStatus,
} from "@/src/runtime-config";

export function BackendStatusBanner() {
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [retrying, setRetrying] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const nextStatus = await invoke<BackendStatus>("backend_status");
      setStatus(nextStatus);
      if (nextStatus.state === "ready") {
        const config = await invoke<BackendRuntimeConfig>("backend_runtime_config");
        const current = getBackendRuntimeSnapshot();
        const backendBaseUrl = config.backendBaseUrl.replace(/\/$/, "");
        if (current.generation !== config.generation || current.backendBaseUrl !== backendBaseUrl) {
          // Compare the supervisor generation, not only the URL: a restarted process may reuse
          // the same port and still requires the Assistant runtime to reconcile its snapshot.
          setBackendBaseUrl(config.backendBaseUrl, { generation: config.generation });
        }
      }
    } catch {
      // Normal browser development does not expose Tauri commands.
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    const initialRefresh = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      window.clearTimeout(initialRefresh);
      window.clearInterval(timer);
    };
  }, [refresh]);

  if (!status || status.state === "ready") return null;

  const retry = async () => {
    setRetrying(true);
    try {
      const config = await invoke<BackendRuntimeConfig>("restart_backend");
      setBackendBaseUrl(config.backendBaseUrl, { generation: config.generation, runtimeChanged: true });
      setStatus(config.status);
    } catch {
      await refresh();
    } finally {
      setRetrying(false);
    }
  };

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
