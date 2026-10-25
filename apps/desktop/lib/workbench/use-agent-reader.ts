import { useCallback, useEffect, useRef, useState } from "react";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import type { TransportState } from "@/lib/assistant/contract";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useWorkbenchAgentStatus } from "@/lib/workbench/agent-status";

type ReaderError = { message: string; missing: boolean };

/**
 * 阅读页的首屏数据与状态摘要。
 *
 * 只在挂载（或用户手动重试）时读一次 canonical snapshot 作为 runtime 的初始状态；之后的内容全部
 * 经 Task 长订阅到达，断流重连由阅读页的 `SubscriptionRecovery` 负责，只重开订阅、不回到这里重读
 * snapshot、不重建 runtime。状态查询只用于工作台标签的状态摘要，订阅提交新活动时顺带刷新它。
 */
export function useAgentReader(taskId: number) {
  const { query, available, refresh } = useWorkbenchAgentStatus(taskId);
  const [snapshot, setSnapshot] = useState<TransportState | null>(null);
  const [error, setError] = useState<ReaderError | null>(null);
  const [revision, setRevision] = useState(0);
  const lastActivityRef = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setSnapshot(null);
    setError(null);
    if (!available) return;
    void fetchCanonicalSnapshot(taskId, { reason: "workbench_child_run", timeoutMs: 15_000, signal: controller.signal })
      .then(outcome => {
        if (outcome.kind === "superseded") return;
        setSnapshot(outcome.snapshot);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        const missing = cause instanceof HttpError && cause.status === 404;
        void frontendLog("WARNING", missing ? "workbench_agent_stale" : "workbench_agent_initial_read_failed",
          missing ? "Workbench 子 Agent 任务已不存在" : "Workbench 子 Agent 首屏读取失败", {
            data: { taskId }, error: cause,
          });
        setError(missing
          ? { message: "子 Agent 任务已不存在", missing: true }
          : { message: userErrorMessage(cause, "无法同步子 Agent，请重试"), missing: false });
        if (missing) refresh();
      });
    return () => controller.abort();
  }, [available, refresh, revision, taskId]);

  const onStateCommit = useCallback((state: TransportState) => {
    const activity = state.activity === null ? null : `${state.activity.runId}:${state.activity.status}`;
    if (activity === lastActivityRef.current) return;
    lastActivityRef.current = activity;
    refresh();
  }, [refresh]);
  /** 首屏读取失败或任务已不存在后，用户手动重新读取。 */
  const retry = useCallback(() => {
    refresh();
    setRevision(value => value + 1);
    void frontendLog("INFO", "workbench_agent_resync_manual", "用户请求重新同步 Workbench 子 Agent", { data: { taskId } });
  }, [refresh, taskId]);

  const missing = query.error instanceof HttpError && query.error.status === 404;
  return { statusError: query.isError, refreshingStatus: query.isFetching, refreshStatus: refresh, snapshot: missing || !available ? null : snapshot, error, missing: missing || error?.missing, available, retry, onStateCommit };
}
