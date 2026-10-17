import { useCallback, useEffect, useRef, useState } from "react";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import { isTerminalRunStatus, type TransportState } from "@/lib/assistant/contract";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useWorkbenchAgentStatus, type AgentRunSummary } from "@/lib/workbench/agent-status";

const RESYNC_DELAYS = [300, 900];
type ReaderError = { message: string; exhausted: boolean; missing: boolean };
const summaryOf = (state: TransportState): AgentRunSummary => ({
  runId: state.current_run_id, status: currentTransportRun(state)?.status ?? null,
});
const sameAgentRun = (a: AgentRunSummary, b: AgentRunSummary) => a.runId === b.runId && a.status === b.status;

/** 查询只发现观察目标变化；消息与状态仍从 canonical snapshot / SSE 读取，不向 Transport 回写摘要。 */
export function agentReaderNeedsSync(observed: AgentRunSummary, expected: AgentRunSummary, streamEnded: boolean): boolean {
  return observed.runId !== expected.runId || (streamEnded && !sameAgentRun(observed, expected));
}

/**
 * 激活阅读页的唯一重同步入口。自动恢复预算按查询发现的目标变化重置，
 * 同一目标的快照滞后、流中断共用有限预算；手动重试与重新挂载开启新预算。
 */
export function useAgentReader(taskId: number) {
  const { query, available, refresh } = useWorkbenchAgentStatus(taskId);
  const [snapshot, setSnapshot] = useState<TransportState | null>(null);
  const [observed, setObserved] = useState<AgentRunSummary | null>(null);
  const [streamEnded, setStreamEnded] = useState(true);
  const [error, setError] = useState<ReaderError | null>(null);
  const [revision, setRevision] = useState(0);
  const attempts = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const expectedRef = useRef<AgentRunSummary | null>(null);
  const observedRef = useRef<AgentRunSummary | null>(null);
  const queryUpdatedAt = useRef(query.dataUpdatedAt);
  queryUpdatedAt.current = query.dataUpdatedAt;
  const queryAtLastCommit = useRef(0);

  const clearRetry = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  const resync = useCallback((cause?: unknown) => {
    setSnapshot(null);
    if (timer.current !== null) return;
    const delay = RESYNC_DELAYS[attempts.current];
    const exhausted = delay === undefined;
    setError({ message: exhausted ? userErrorMessage(cause, "无法同步子 Agent，请重试") : "子 Agent 连接中断，正在重新同步…", exhausted, missing: false });
    if (exhausted) return;
    attempts.current += 1;
    void frontendLog("WARNING", "workbench_agent_resync_scheduled", "Workbench 子 Agent 计划有限重同步", {
      data: { taskId, attempt: attempts.current }, error: cause,
    });
    timer.current = setTimeout(() => {
      timer.current = null;
      setRevision(value => value + 1);
    }, delay);
  }, [taskId]);

  useEffect(() => {
    if (!query.isSuccess) return;
    const expected = query.data;
    if (!expectedRef.current || !sameAgentRun(expectedRef.current, expected)) {
      expectedRef.current = expected;
      attempts.current = 0;
      if (error?.exhausted && !error.missing) {
        setRevision(value => value + 1);
        return;
      }
    }
    // SSE 已推进时，等待本次提示之后的 HTTP 结果，旧摘要不能把终态阅读页拉回运行中。
    if (query.dataUpdatedAt <= queryAtLastCommit.current) return;
    if (snapshot && observed && agentReaderNeedsSync(observed, expected, streamEnded)) resync();
  }, [error, observed, query.data, query.dataUpdatedAt, query.isSuccess, resync, snapshot, streamEnded]);

  useEffect(() => {
    const controller = new AbortController();
    setSnapshot(null);
    setError(null);
    if (!available) {
      attempts.current = 0;
      clearRetry();
      return;
    }
    void fetchCanonicalSnapshot(taskId, { reason: "workbench_child_run", timeoutMs: 15_000, signal: controller.signal })
      .then(outcome => {
        if (outcome.kind === "superseded") return;
        const summary = summaryOf(outcome.snapshot);
        observedRef.current = summary;
        setObserved(summary);
        setStreamEnded(summary.runId === null || isTerminalRunStatus(summary.status));
        setSnapshot(outcome.snapshot);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        if (cause instanceof HttpError && cause.status === 404) {
          void frontendLog("WARNING", "workbench_agent_stale", "Workbench 子 Agent 任务已不存在", { data: { taskId } });
          setError({ message: "子 Agent 任务已不存在", exhausted: true, missing: true });
          refresh();
          return;
        }
        resync(cause);
      });
    return () => controller.abort();
  }, [available, clearRetry, refresh, resync, revision, taskId]);

  useEffect(() => clearRetry, [clearRetry]);

  const onStateCommit = useCallback((state: TransportState) => {
    const next = summaryOf(state);
    if (observedRef.current && sameAgentRun(observedRef.current, next)) return;
    observedRef.current = next;
    queryAtLastCommit.current = queryUpdatedAt.current;
    setObserved(next);
    refresh();
  }, [refresh]);
  const onFinish = useCallback(({ targetRunStatus }: { targetRunStatus: string | null }) => {
    setStreamEnded(true);
    refresh();
    // 正常终态保留阅读内容；意外 EOF / resync_required 必须回 canonical 边界恢复。
    if (!isTerminalRunStatus(targetRunStatus)) resync();
  }, [refresh, resync]);
  const retry = useCallback(() => {
    clearRetry();
    attempts.current = 0;
    setError(null);
    refresh();
    setRevision(value => value + 1);
    void frontendLog("INFO", "workbench_agent_resync_manual", "用户请求重新同步 Workbench 子 Agent", { data: { taskId } });
  }, [clearRetry, refresh, taskId]);

  const missing = query.error instanceof HttpError && query.error.status === 404;
  return { statusError: query.isError, refreshingStatus: query.isFetching, refreshStatus: refresh, snapshot: missing || !available ? null : snapshot, error, missing: missing || error?.missing, available, retry, resync, onStateCommit, onFinish };
}
