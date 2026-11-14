import { markSessionRequestsUnsynced, reconcileSessionStatuses } from "@/lib/assistant/command-request-store";
import { useEffect, useRef } from "react";

import { readNdjson } from "@/lib/http/ndjson";
import { requestOk } from "@/lib/http/client";
import { frontendLog } from "@/lib/logging/frontend-log";
import { type SessionStatus, useSessionStatusStore } from "@/lib/session-status/session-status-store";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";

export type SessionFeedFrame =
  | { type: "full" | "changed"; sessions: Record<number, SessionStatus> }
  | { type: "catalog"; workspaces: boolean; workspaceIds: number[] }
  | { type: "heartbeat" };

/** 目录失效交给目录的 owner（`useWorkspaceCatalog`）；两者都应是稳定的回调。 */
export type SessionFeedCatalogHandlers = {
  /** 工作区列表变了，或重连后要整体重读。 */
  onWorkspacesChanged: () => void;
  /** 这个工作区的侧栏 Session 变了。 */
  onWorkspaceSessionsChanged: (workspaceId: number) => void;
};

/**
 * 应用级唯一的 Session 推送连接（`GET /session-feed/stream`），在 `AppLayout` 挂一次。送两类事实：
 *
 * - 状态：后端在 Run 开始与状态迁移、工具调用状态迁移（等你操作的增减）时推送变化，并每隔一段时间
 *   重发整表兜住漏掉的变化；写进 `session-status-store`。
 * - 目录失效：目录的写入方（建删工作区与 Session、改标题、开新 Run）提交后推送，交给目录重读。目录
 *   只从这里得知后端的变化（含后台会话 的分叉与标题）。断开期间漏掉的失效不会重发，所以重连后
 *   收到第一个整表帧时整体重读一次目录；首次连接不重读，目录挂载时自己加载。
 *
 * 断开后按退避重连，后端换代或恢复可用时重开；断开期间状态 store 保留旧值并标为未连接。
 */
export function useSessionFeed(catalog: SessionFeedCatalogHandlers): void {
  const backend = useBackendRuntime();
  const catalogRef = useRef(catalog);
  catalogRef.current = catalog;
  // 跨连接（含后端换代）记住是否连上过：只有之后的连接才可能漏掉失效。
  const connectedBeforeRef = useRef(false);

  useEffect(() => {
    const store = useSessionStatusStore.getState();
    if (!backend.available) {
      store.setConnected(false);
      markSessionRequestsUnsynced();
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const connect = async () => {
      let resyncCatalog = connectedBeforeRef.current;
      const onFrame = (frame: SessionFeedFrame) => {
        attempts = 0;
        if (frame.type === "full" || frame.type === "changed") reconcileSessionStatuses(frame.sessions);
        if (frame.type === "full") {
          store.replaceAll(frame.sessions);
          if (resyncCatalog) {
            resyncCatalog = false;
            catalogRef.current.onWorkspacesChanged();
          }
        } else if (frame.type === "changed") {
          store.merge(frame.sessions);
        } else if (frame.type === "catalog") {
          // 工作区列表重读已包含全部工作区的 Session，不再逐个刷新。
          if (frame.workspaces) catalogRef.current.onWorkspacesChanged();
          else for (const workspaceId of frame.workspaceIds) catalogRef.current.onWorkspaceSessionsChanged(workspaceId);
        }
      };
      try {
        const response = await requestOk("/session-feed/stream", {
          signal: controller.signal,
          headers: { Accept: "application/x-ndjson" },
        });
        store.setConnected(true);
        connectedBeforeRef.current = true;
        await readNdjson(response, onFrame, parseSessionFeedFrame);
        if (!controller.signal.aborted) throw new Error("Session 推送连接已断开");
      } catch (error) {
        if (controller.signal.aborted) return;
        store.setConnected(false);
        markSessionRequestsUnsynced();
        void frontendLog("WARNING", "session_feed_disconnected", "Session 推送中断，稍后重连", {
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

/** 校验一行推送；`sessions` 的键是 Session id（JSON 里是字符串）。 */
export function parseSessionFeedFrame(value: unknown): SessionFeedFrame {
  if (typeof value !== "object" || value === null) throw new Error("session feed frame must be an object");
  const frame = value as Record<string, unknown>;
  if (frame.type === "heartbeat") return { type: "heartbeat" };
  if (frame.type === "catalog") {
    if (typeof frame.workspaces !== "boolean") throw new Error("session feed catalog workspaces is invalid");
    if (!Array.isArray(frame.workspaceIds) || !frame.workspaceIds.every(isPositiveInteger)) {
      throw new Error("session feed catalog workspaceIds is invalid");
    }
    return { type: "catalog", workspaces: frame.workspaces, workspaceIds: frame.workspaceIds };
  }
  if (frame.type !== "full" && frame.type !== "changed") throw new Error("session feed frame type is invalid");
  if (typeof frame.sessions !== "object" || frame.sessions === null) throw new Error("session feed frame sessions is invalid");
  const sessions: Record<number, SessionStatus> = {};
  for (const [key, entry] of Object.entries(frame.sessions)) {
    const sessionId = Number(key);
    if (!isPositiveInteger(sessionId)) throw new Error("session feed frame session id is invalid");
    if (typeof entry !== "object" || entry === null) throw new Error("session feed frame entry is invalid");
    const { executionStatus, awaitingUserCalls } = entry as Record<string, unknown>;
    if (executionStatus !== null && typeof executionStatus !== "string") throw new Error("session feed frame status is invalid");
    if (typeof awaitingUserCalls !== "number" || !Number.isInteger(awaitingUserCalls) || awaitingUserCalls < 0) {
      throw new Error("session feed frame awaitingUserCalls is invalid");
    }
    sessions[sessionId] = { executionStatus, awaitingUserCalls };
  }
  return { type: frame.type, sessions };
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}
