import { useEffect, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { readFileStream, type WatchEvent } from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { frontendLog } from "@/lib/logging/frontend-log";
import {
  getBackendRuntimeSnapshot,
  subscribeBackendRuntime,
} from "@/src/runtime-config";

/** 每次重连先重新读取；不依赖文件系统事件完整重放，端口换代会取消旧请求。 */
export function useFileWatch(id: number, paths: string[]) {
  const client = useQueryClient();
  const backend = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendRuntimeSnapshot,
    getBackendRuntimeSnapshot,
  );
  const pathKey = JSON.stringify([...new Set(paths)].sort());
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const refresh = () => {
      void client.invalidateQueries({ queryKey: queryKeys.files(id) });
    };
    const connect = async () => {
      try {
        await readFileStream<WatchEvent>(
          id,
          "watch",
          { paths: JSON.parse(pathKey) },
          controller.signal,
          (event) => {
            if (event.type === "closed") {
              controller.abort();
              return;
            }
            attempts = 0;
            if (event.type !== "heartbeat") refresh();
          },
        );
        if (!controller.signal.aborted) throw new Error("文件监听连接已断开");
      } catch (error) {
        if (controller.signal.aborted) return;
        void frontendLog(
          "WARNING",
          "workspace_watch_disconnected",
          "文件监听中断",
          { data: { workspaceId: id }, error },
        );
        timer = setTimeout(
          () => void connect(),
          Math.min(30_000, 1000 * 2 ** attempts++),
        );
      }
    };
    void connect();
    window.addEventListener("focus", refresh);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("focus", refresh);
    };
  }, [id, pathKey, client, backend]);
}
