import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import type { TransportState } from "@/lib/assistant/contract";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";

type ReaderError = { message: string; missing: boolean };

/**
 * 阅读页的首屏快照。
 *
 * 只在挂载（或用户手动重试）时读一次 canonical snapshot 作为 runtime 的初始状态；之后的内容全部
 * 经 Task 长订阅到达，断流重连由阅读页的 `SubscriptionRecovery` 负责，只重开订阅、不回到这里重读
 * snapshot、不重建 runtime。运行状态不在这里读：标签与侧栏共用应用级的 Task 状态推送，阅读页内的
 * 状态就是订阅里的当前活动。
 */
export function useAgentReader(taskId: number) {
  const backend = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const available = backend.available;
  const [snapshot, setSnapshot] = useState<TransportState | null>(null);
  const [error, setError] = useState<ReaderError | null>(null);
  const [revision, setRevision] = useState(0);

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
      });
    return () => controller.abort();
  }, [available, revision, taskId]);

  /** 首屏读取失败或任务已不存在后，用户手动重新读取。 */
  const retry = useCallback(() => {
    setRevision(value => value + 1);
    void frontendLog("INFO", "workbench_agent_resync_manual", "用户请求重新同步 Workbench 子 Agent", { data: { taskId } });
  }, [taskId]);

  return { snapshot: available ? snapshot : null, error, missing: error?.missing ?? false, available, retry };
}
