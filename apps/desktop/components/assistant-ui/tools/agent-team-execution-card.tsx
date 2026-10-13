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

  useEffect(() => {
    if (!display) return undefined;
    let disposed = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const current = await getAgentTeamRun(display.teamRunId);
        if (disposed) return;
        setRun(current);
        if (current.status === "pending" || current.status === "running") {
          timer = window.setTimeout(() => void poll(), 1000);
        }
      } catch {
        if (!disposed) {
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
      backendStatus={artifact.backendStatus}
      onOpenAgent={(taskId, title, role) => actions?.openAgent({ taskId, title, role })}
    />
  );
}

type AgentTeamExecutionContentProps = {
  display: AgentTeamRunDisplay;
  run: AgentTeamRun | null;
  backendStatus: ToolArtifact["backendStatus"];
  onOpenAgent: (taskId: number, title: string, role: string) => void;
};

/** 只渲染 TeamRun 查询投影；轮询与卡片状态不参与节点事实判断。 */
export function AgentTeamExecutionContent({
  display, run, backendStatus, onOpenAgent,
}: AgentTeamExecutionContentProps) {
  const currentStatus = run ? RUN_STATUS_LABELS[run.status] ?? run.status : "读取进度中";

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

      {run && (
        <AgentTeamExecutionPipeline run={run} onOpenAgent={onOpenAgent} />
      )}
    </section>
  );
}
