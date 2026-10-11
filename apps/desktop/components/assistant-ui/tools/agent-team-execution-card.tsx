import { useEffect, useState } from "react";
import { UsersIcon } from "lucide-react";

import { getAgentTeamRun, type AgentTeamRun } from "@/lib/api/agent-teams";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { readToolArtifact, type ToolArtifact } from "./types";
import { readAgentTeamRunDisplay } from "./agent-team-display";
import { ToolStatus } from "./tool-status";
import type { ToolPartRendererProps } from "./tool-renderer-registry";
import type { AgentTeamRunDisplay } from "./agent-team-display";
import { AgentTeamExecutionPipeline } from "./agent-team-execution-pipeline";

type AgentTeamExecutionCardProps = ToolPartRendererProps & {
  displayOverride?: AgentTeamRunDisplay;
};

const END_REASON_LABELS: Record<string, string> = {
  user_rejected: "方案已驳回",
  superseded_by_new_preview: "方案已被新方案替代",
  team_start_failed: "Team 启动失败",
  transition_not_found: "没有匹配的节点转移",
  runtime_unavailable: "本地运行时不可用",
  next_node_start_failed: "下一节点启动失败",
  node_run_failed: "节点执行失败",
  node_run_cancelled: "节点执行已取消",
  node_output_invalid: "节点结果无效",
  implicit_completion_missing: "节点未提交 Team 状态",
  cancelled: "Team 已取消",
  runtime_restarted: "后端重启后已取消",
  max_runs_exceeded: "达到最大节点执行次数",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  pending: "等待确认",
  running: "执行中",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

export function AgentTeamExecutionCard(props: AgentTeamExecutionCardProps) {
  const artifact = readToolArtifact(props.artifact);
  const display = props.displayOverride ?? readAgentTeamRunDisplay(artifact.display_data)
    ?? (artifact.team_run_id !== undefined
      ? { teamRunId: artifact.team_run_id, teamId: "", goal: "" }
      : null);
  const actions = useWorkbenchActions();
  const [run, setRun] = useState<AgentTeamRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!display) return undefined;
    let disposed = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const current = await getAgentTeamRun(display.teamRunId);
        if (disposed) return;
        setRun(current);
        setError(null);
        if (current.status === "pending" || current.status === "running") {
          timer = window.setTimeout(() => void poll(), 1000);
        }
      } catch (cause) {
        if (!disposed) {
          setError(cause instanceof Error ? cause.message : "读取 Team 进度失败");
          timer = window.setTimeout(() => void poll(), 2000);
        }
      }
    };
    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [display?.teamRunId]);

  if (!display) return null;
  return (
    <AgentTeamExecutionContent
      display={display}
      run={run}
      error={error}
      backendStatus={artifact.backendStatus}
      onOpenAgent={(taskId, title, role) => actions?.openAgent({ taskId, title, role })}
    />
  );
}

type AgentTeamExecutionContentProps = {
  display: AgentTeamRunDisplay;
  run: AgentTeamRun | null;
  error: string | null;
  backendStatus: ToolArtifact["backendStatus"];
  onOpenAgent: (taskId: number, title: string, role: string) => void;
};

/** 只渲染 TeamRun 查询投影；轮询与卡片状态不参与节点事实判断。 */
export function AgentTeamExecutionContent({
  display, run, error, backendStatus, onOpenAgent,
}: AgentTeamExecutionContentProps) {
  const currentStatus = run ? RUN_STATUS_LABELS[run.status] ?? run.status : "读取进度中";
  const endReasonLabel = run?.end_reason ? END_REASON_LABELS[run.end_reason] : undefined;
  const failedNode = run?.nodes.find((node) => node.node_id === run.failure_node_id);
  const nodeFailure = failedNode?.visits.at(-1)?.failure_message;
  const failureDescription = nodeFailure || endReasonLabel || "具体原因未记录，可打开节点任务查看执行记录";

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
      </header>

      {run?.status === "failed" && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2" role="status">
          <p className="text-sm font-medium text-destructive">
            {failedNode ? `节点「${failedNode.name}」失败` : "Agent Team 执行失败"}
          </p>
          <p className="mt-1 max-h-28 overflow-y-auto break-words text-xs leading-relaxed">{failureDescription}</p>
          <p className="mt-1 text-[10px] text-muted-foreground">TeamRun #{run.id}{failedNode?.visits.at(-1)?.conversation_run_id ? ` · 节点 Run #${failedNode.visits.at(-1)?.conversation_run_id}` : ""}</p>
        </div>
      )}

      {run && (
        <AgentTeamExecutionPipeline run={run} onOpenAgent={onOpenAgent} />
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}
      {run?.end_reason && run.status !== "failed" && (
        endReasonLabel ? (
          <p className="text-xs text-muted-foreground">{endReasonLabel}</p>
        ) : (
          <div className="rounded-lg border border-border/60 bg-muted/10 px-2.5 py-2">
            <p className="mb-1 text-[10px] font-medium text-muted-foreground">最终结果</p>
            <p className="max-h-48 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed">{run.end_reason}</p>
          </div>
        )
      )}
    </section>
  );
}
