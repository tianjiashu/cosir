import { cn } from "@/lib/utils";
import { AgentTeamApproval } from "../agent-team-tool";
import { AgentTeamExecutionCard, runStatusLabel, useAgentTeamRunPolling } from "../agent-team-execution-card";
import { readAgentTeamPreviewDisplay } from "../agent-team-display";
import type { CallView } from "../call-view";
import { DetailSurface } from "../details/detail-view";

/** TeamRun 自身的异常（失败、驳回、停止）：工具 `completed` 只表示结论已交付；执行中由标题扫光表达。 */
function TeamProblem({ id }: { id: number; }) {
  const query = useAgentTeamRunPolling(id);
  if (query.isError) return <span>状态同步失败</span>;
  const run = query.data;
  if (run?.status !== "failed" && run?.status !== "cancelled") return null;
  return <span className={cn(run.status === "failed" && "text-destructive")}>{runStatusLabel(run)}</span>;
}

export const teamView: CallView = (data, context) => {
  const preview = readAgentTeamPreviewDisplay(data);
  if (!preview) return null;
  if (context.inherited) return {
    meta: preview.name,
    detail: { render: () => <DetailSurface className="text-muted-foreground whitespace-pre-wrap break-words">{preview.goal}</DetailSurface> },
  };
  return {
    status: <TeamProblem id={preview.teamRunId} />,
    detail: context.artifact.awaitingUser || context.surface === "details" ? {
      defaultOpen: true,
      render: () => context.artifact.awaitingUser ? <AgentTeamApproval {...context} /> : <AgentTeamExecutionCard {...context} displayOverride={preview} />,
    } : undefined,
  };
};
