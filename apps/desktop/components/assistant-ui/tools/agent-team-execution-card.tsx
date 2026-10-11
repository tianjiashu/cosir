import { useEffect, useState } from "react";
import { UsersIcon } from "lucide-react";

import { getAgentTeamRun, type AgentTeamRun } from "@/lib/api/agent-teams";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { readToolArtifact } from "./types";
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
  const display = props.displayOverride ?? readAgentTeamRunDisplay(artifact.display_data);
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
  const currentStatus = run ? RUN_STATUS_LABELS[run.status] ?? run.status : "读取进度中";
  const openAgent = (taskId: number, title: string, role: string) => {
    actions?.openAgent({ taskId, title, role });
  };
  const endReasonLabel = run?.end_reason ? END_REASON_LABELS[run.end_reason] : undefined;

  return (
    <section className="space-y-3 rounded-xl border border-border/70 bg-card p-3 shadow-sm" aria-label="Agent Team 执行进度">
      <header className="flex items-start gap-2">
        <UsersIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{display.teamId}</span>
            <ToolStatus status={artifact.backendStatus} className="text-muted-foreground" />
            <span className="text-xs text-muted-foreground">{currentStatus}</span>
          </div>
          {(display.goal || run?.goal) && (
            <p className="mt-1 line-clamp-2 text-xs text-muted-foreground" title={display.goal || run?.goal || undefined}>
              {display.goal || run?.goal}
            </p>
          )}
        </div>
      </header>

      {run && (
        <AgentTeamExecutionPipeline run={run} onOpenAgent={openAgent} />
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}
      {run?.end_reason && (
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
