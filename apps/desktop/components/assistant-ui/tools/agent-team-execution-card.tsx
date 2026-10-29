import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { UsersIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { getAgentTeamRun, type AgentTeamRun } from "@/lib/api/agent-teams";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { queryKeys } from "@/lib/query/query-keys";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { readToolArtifact, type ToolArtifact } from "./types";
import { readAgentTeamRunDisplay } from "./agent-team-display";
import { ToolStatus } from "./tool-status";
import type { ToolPartRendererProps } from "./tool-renderer-registry";
import type { AgentTeamRunDisplay } from "./agent-team-display";
import { AgentTeamExecutionPipeline } from "./agent-team-execution-pipeline";
import { ToolStopButton, useStopRequest } from "./tool-stop-button";

type AgentTeamExecutionCardProps = ToolPartRendererProps & {
  displayOverride?: AgentTeamRunDisplay;
};

const RUN_STATUS_LABELS: Record<string, string> = {
  pending: "等待确认",
  running: "执行中",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

/** 取消按原因细分：用户单独停止与随对话停止对用户是两件事，其余取消原因沿用「已取消」。 */
const CANCELLED_REASON_LABELS: Record<string, string> = {
  cancelled: "已停止",
  parent_cancelled: "随对话停止",
};

function runStatusLabel(run: AgentTeamRun): string {
  if (run.status === "cancelled" && run.end_reason) {
    const label = CANCELLED_REASON_LABELS[run.end_reason];
    if (label) return label;
  }
  return RUN_STATUS_LABELS[run.status] ?? run.status;
}

/**
 * 是否提供单独停止 Team 的入口：只在 TeamRun 运行中、可写面板、且用户没在停止整个 Task 时。
 * 待确认阶段不在这里——审批卡有「驳回」。
 */
export function canStopTeamRun(
  run: AgentTeamRun | null,
  interactive: boolean,
  taskCancelling: boolean,
): boolean {
  return run?.status === "running" && interactive && !taskCancelling;
}

const POLL_INTERVAL_MS = 1_000;
const MAX_POLL_RETRIES = 4;
const MAX_RETRY_DELAY_MS = 16_000;

function isActiveRun(status: AgentTeamRun["status"] | undefined): boolean {
  return status === "pending" || status === "running";
}

/**
 * 轮询 TeamRun 投影，直到它进入终态。
 *
 * 失败策略：单次读取失败按指数退避重试有限次；4xx（含 404：TeamRun 已不存在）是确定性结果，
 * 不重试。重试耗尽后查询进入 error 态，refetchInterval 随之停止——不能只看缓存里的旧
 * status，否则仍是 running 的旧数据会让失败查询每秒重拉、永不停止。用户可从卡片手动重试。
 */
export function useAgentTeamRunPolling(teamRunId: number | undefined) {
  const query = useQuery({
    queryKey: queryKeys.agentTeamRun(teamRunId ?? 0),
    queryFn: ({ signal }) => getAgentTeamRun(teamRunId as number, { signal }),
    enabled: teamRunId !== undefined,
    refetchInterval: (current) => (
      current.state.status !== "error" && isActiveRun(current.state.data?.status) ? POLL_INTERVAL_MS : false
    ),
    retry: (failureCount, error) => {
      if (error instanceof HttpError && error.status >= 400 && error.status < 500) return false;
      return failureCount < MAX_POLL_RETRIES;
    },
    retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, MAX_RETRY_DELAY_MS),
  });

  const { error } = query;
  useEffect(() => {
    if (!error) return;
    void frontendLog("WARNING", "agent_team_run_poll_failed", "读取 Agent Team 执行进度失败，已停止轮询", {
      data: {
        teamRunId,
        status: error instanceof HttpError ? error.status : null,
      },
      error,
    });
  }, [error, teamRunId]);

  return query;
}

export function AgentTeamExecutionCard(props: AgentTeamExecutionCardProps) {
  const { taskId, toolCallId, interactive = false, taskCancelling = false } = props;
  const artifact = readToolArtifact(props.artifact);
  const display = props.displayOverride ?? readAgentTeamRunDisplay(artifact.display_data)
    ?? (artifact.team_run_id !== undefined
      ? { teamRunId: artifact.team_run_id, teamId: "", goal: "" }
      : null);
  const actions = useWorkbenchActions();
  const runQuery = useAgentTeamRunPolling(display?.teamRunId);
  const run = runQuery.data ?? null;
  const canStop = canStopTeamRun(run, interactive, taskCancelling);
  // 「停止中」保持到轮询看到 TeamRun 离开 running：HTTP 只确认取消信号已发出，不乐观改状态。
  const cancellation = useStopRequest(run?.status === "running");

  const requestStop = () => {
    if (!canStop) return;
    void frontendLog("INFO", "agent_team_cancel_requested", "用户请求单独停止 Agent Team", {
      data: { taskId, toolCallId, teamRunId: display?.teamRunId },
    });
    void cancellation.stop(async () => {
      // 单独停止 Team 就是取消这次 agent_team 调用；Coordinator 读到工具级信号后落定 TeamRun。
      const result = await cancelToolCall(taskId, toolCallId);
      return result.kind === "failed" ? result.message : null;
    });
  };

  if (!display) return null;
  return (
    <AgentTeamExecutionContent
      display={display}
      run={run}
      backendStatus={artifact.backendStatus}
      pollError={runQuery.isError ? userErrorMessage(runQuery.error, "请稍后重试") : null}
      onRetryPoll={() => void runQuery.refetch()}
      onOpenAgent={(childTaskId, title) => actions?.openAgent({ taskId: childTaskId, title })}
      canStop={canStop}
      stopping={cancellation.pending}
      stopError={cancellation.error}
      onStop={requestStop}
    />
  );
}

type AgentTeamExecutionContentProps = {
  display: AgentTeamRunDisplay;
  run: AgentTeamRun | null;
  backendStatus: ToolArtifact["backendStatus"];
  /** 轮询已放弃时的可读原因；为 null 表示仍在轮询或已正常结束。 */
  pollError: string | null;
  onRetryPoll: () => void;
  onOpenAgent: (taskId: number, title: string) => void;
  /** 是否显示停止按钮（见 {@link canStopTeamRun}）。 */
  canStop?: boolean;
  stopping?: boolean;
  /** 停止请求被拒或网络失败时的可读原因。 */
  stopError?: string | null;
  onStop?: () => void;
};

/** 只渲染 TeamRun 查询投影；轮询与卡片状态不参与节点事实判断。 */
export function AgentTeamExecutionContent({
  display, run, backendStatus, pollError, onRetryPoll, onOpenAgent,
  canStop = false, stopping = false, stopError = null, onStop,
}: AgentTeamExecutionContentProps) {
  const currentStatus = run
    ? runStatusLabel(run)
    : pollError ? "进度不可用" : "读取进度中";

  return (
    <section className="space-y-3 rounded-xl border border-border/70 bg-card p-3 shadow-sm" aria-label="Agent Team 执行进度">
      <header className="flex items-start gap-2">
        <UsersIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{run?.team_id || display.teamId || "Agent Team"}</span>
            {!run && <ToolStatus status={backendStatus} className="text-muted-foreground" />}
            <span className="text-xs text-muted-foreground">{currentStatus}</span>
          </div>
          {(run?.goal || display.goal) && (
            <p className="mt-1 line-clamp-2 text-xs text-muted-foreground" title={run?.goal || display.goal}>
              {run?.goal || display.goal}
            </p>
          )}
        </div>
        {(canStop || stopping) && onStop && (
          <ToolStopButton label="Agent Team" pending={stopping} onClick={onStop} className="shrink-0" />
        )}
      </header>

      {stopError && (
        <p className="text-xs text-destructive" role="alert">停止 Agent Team 失败：{stopError}</p>
      )}

      {pollError && (
        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground" role="status">
          <span className="min-w-0">执行进度读取失败：{pollError}</span>
          <Button type="button" variant="ghost" size="sm" onClick={onRetryPoll}>重试</Button>
        </div>
      )}

      {run && (
        <AgentTeamExecutionPipeline run={run} onOpenAgent={onOpenAgent} />
      )}
    </section>
  );
}
