import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { UsersIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { getAgentTeamRun, type AgentTeamRun } from "@/lib/api/agent-teams";
import { stopTarget } from "@/lib/assistant/command-request-store";
import { cancelAgentTeamRun } from "@/lib/assistant/task-commands";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/http-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { queryKeys } from "@/lib/query/query-keys";
import { useWorkbenchActions } from "@/lib/workbench/workbench-context";
import { SURFACE_CARD_CLASS } from "@/components/conversation/turn/turn-tokens";
import { cn } from "@/lib/utils";
import type { ArtifactCardProps } from "../artifact-card-props";
import type { AgentTeamRunDisplay } from "./agent-team-display";
import { AgentTeamExecutionPipeline } from "./agent-team-execution-pipeline";
import { ToolStopButton, useStopRequest } from "../tool-stop-button";

type AgentTeamExecutionCardProps = ArtifactCardProps & {
  /** 来自审批方案的展示数据（批准后的执行卡没有自己的展示 `kind`，按 `team_run_id` 读取 TeamRun）。 */
  display: AgentTeamRunDisplay;
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
  user_rejected: "已驳回",
};

export function runStatusLabel(run: AgentTeamRun): string {
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

/**
 * 运行中轮询 TeamRun 投影（节点进度随时在变），离开运行即停；每次挂载都读一次。
 *
 * 待确认（pending）不轮询：它只会被用户的决定、撤回这一回合（卡片随之卸载）或删除 Task 改变。决定被接受后
 * snapshot 里这次调用不再等你，同一行从审批表单换成执行卡——换卡就是状态变了的时刻，所以挂载时总是读一次
 * （不受全局 staleTime 限制）；不能只靠提交后的那次刷新：表单卸载时它会随观察者一起被取消。
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
    refetchOnMount: "always",
    refetchInterval: (current) => (
      current.state.status !== "error" && current.state.data?.status === "running" ? POLL_INTERVAL_MS : false
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
  const { taskId, toolCallId, display, interactive = false, taskCancelling = false } = props;
  const actions = useWorkbenchActions();
  const runQuery = useAgentTeamRunPolling(display.teamRunId);
  const run = runQuery.data ?? null;
  const canStop = canStopTeamRun(run, interactive, taskCancelling);
  // 停止槽位是调用级的、不对账（见 command-request-store）：HTTP 只确认取消信号已发出，「停止中」
  // 靠只在 TeamRun 运行中渲染按钮收尾——轮询看到它离开 running，按钮与失败提示随之消失。
  const cancellation = useStopRequest(taskId, stopTarget(toolCallId));

  const requestStop = () => {
    if (!canStop) return;
    void frontendLog("INFO", "agent_team_cancel_requested", "用户请求单独停止 Agent Team", {
      data: { taskId, toolCallId, teamRunId: display.teamRunId },
    });
    // Team 是编排实体：按 TeamRun 停止；节点与档案收尾后 TeamRun 落定，主 Task 得到一条停止通知。
    void cancellation.stop(() => cancelAgentTeamRun(taskId, toolCallId, display.teamRunId));
  };

  return (
    <AgentTeamExecutionContent
      display={display}
      run={run}
      pollError={runQuery.isError ? userErrorMessage(runQuery.error, "请稍后重试") : null}
      onRetryPoll={() => void runQuery.refetch()}
      onOpenAgent={(childTaskId, title) => actions?.openAgent({ taskId: childTaskId, title })}
      stop={canStop ? { pending: cancellation.pending, error: cancellation.error, onStop: requestStop } : undefined}
    />
  );
}

type AgentTeamExecutionContentProps = {
  display: AgentTeamRunDisplay;
  run: AgentTeamRun | null;
  /** 轮询已放弃时的可读原因；为 null 表示仍在轮询或已正常结束。 */
  pollError: string | null;
  onRetryPoll: () => void;
  onOpenAgent: (taskId: number, title: string) => void;
  /** 停止入口，只在可以停止时给（见 {@link canStopTeamRun}）：没有就不显示按钮与停止失败提示。 */
  stop?: TeamStopControl;
};

type TeamStopControl = {
  pending: boolean;
  /** 停止请求被拒或网络失败时的可读原因。 */
  error: string | null;
  onStop: () => void;
};

/** 只渲染 TeamRun 查询投影；轮询与卡片状态不参与节点事实判断。 */
export function AgentTeamExecutionContent({
  display, run, pollError, onRetryPoll, onOpenAgent, stop,
}: AgentTeamExecutionContentProps) {
  const currentStatus = run
    ? runStatusLabel(run)
    : pollError ? "进度不可用" : "读取进度中";

  return (
    <section className={cn(SURFACE_CARD_CLASS, "space-y-3 p-3")} aria-label="Agent Team 执行进度">
      <header className="flex items-start gap-2">
        <UsersIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{run?.team_id || display.teamId || "Agent Team"}</span>
            <span className="text-xs text-muted-foreground">{currentStatus}</span>
          </div>
          {(run?.goal || display.goal) && (
            <p className="mt-1 line-clamp-2 text-xs text-muted-foreground" title={run?.goal || display.goal}>
              {run?.goal || display.goal}
            </p>
          )}
        </div>
        {stop && (
          <ToolStopButton label="Agent Team" pending={stop.pending} onClick={stop.onStop} className="shrink-0" />
        )}
      </header>

      {stop?.error && (
        <p className="text-xs text-destructive" role="alert">停止 Agent Team 失败：{stop.error}</p>
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
