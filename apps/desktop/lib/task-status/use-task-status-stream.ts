import { useEffect, useSyncExternalStore } from "react";

import { readNdjson } from "@/lib/http/ndjson";
import { requestOk } from "@/lib/http/client";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useTaskStatusStore } from "@/lib/task-status/task-status-store";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";

type StatusFrame =
  | { type: "full" | "changed"; statuses: Record<number, string | null> }
  | { type: "heartbeat" };

/**
 * 应用级唯一的 Task 状态推送连接（`GET /task-status/stream`），在 `AppLayout` 挂一次。
 *
 * 后端在 Run 开始与状态迁移时推送变化，并每隔一段时间重发整表兜住漏掉的变化；这里只把帧写进
 * `task-status-store`。断开后按退避重连，后端换代或恢复可用时重开；断开期间 store 保留旧值并标为
 * 未连接。
 */
export function useTaskStatusStream(): void {
  const backend = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);

  useEffect(() => {
    const store = useTaskStatusStore.getState();
    if (!backend.available) {
      store.setConnected(false);
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const onFrame = (frame: StatusFrame) => {
      attempts = 0;
      if (frame.type === "full") store.replaceAll(frame.statuses);
      else if (frame.type === "changed") store.merge(frame.statuses);
    };
    const connect = async () => {
      try {
        const response = await requestOk("/task-status/stream", {
          signal: controller.signal,
          headers: { Accept: "application/x-ndjson" },
        });
        store.setConnected(true);
        await readNdjson(response, onFrame, parseStatusFrame);
        if (!controller.signal.aborted) throw new Error("状态推送连接已断开");
      } catch (error) {
        if (controller.signal.aborted) return;
        store.setConnected(false);
        void frontendLog("WARNING", "task_status_stream_disconnected", "任务状态推送中断，稍后重连", {
          data: { attempts, generation: backend.generation },
          error,
        });
        timer = setTimeout(() => void connect(), Math.min(30_000, 1000 * 2 ** attempts++));
      }
    };
    void connect();
    return () => {
      controller.abort();
      clearTimeout(timer);
      store.setConnected(false);
    };
  }, [backend.available, backend.generation]);
}

/** 校验一行推送；`statuses` 的键是 Task id（JSON 里是字符串）。 */
export function parseStatusFrame(value: unknown): StatusFrame {
  if (typeof value !== "object" || value === null) throw new Error("task status frame must be an object");
  const frame = value as Record<string, unknown>;
  if (frame.type === "heartbeat") return { type: "heartbeat" };
  if (frame.type !== "full" && frame.type !== "changed") throw new Error("task status frame type is invalid");
  if (typeof frame.statuses !== "object" || frame.statuses === null) throw new Error("task status frame statuses is invalid");
  const statuses: Record<number, string | null> = {};
  for (const [key, status] of Object.entries(frame.statuses)) {
    const taskId = Number(key);
    if (!Number.isInteger(taskId) || taskId <= 0) throw new Error("task status frame task id is invalid");
    if (status !== null && typeof status !== "string") throw new Error("task status frame status is invalid");
    statuses[taskId] = status;
  }
  return { type: frame.type, statuses };
}
