import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { readFileStream, type WatchEvent } from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";

/**
 * 一个工作区的文件变化订阅：后端按周期比对登记的路径，连上（`ready`）与每次变化（`changed`）都整体重读该工作区的
 * 文件查询；重连同样先重读，不依赖变化的完整重放。端口换代会取消旧请求。
 */
export function useFileWatch(id: number, paths: string[]) {
  const client = useQueryClient();
  const backend = useBackendRuntime();
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
          () => {
            attempts = 0;
            refresh();
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
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, pathKey, client, backend]);
}
