import { cn } from "@/lib/utils";
import { AgentTeamApproval } from "../agent-team/agent-team-tool";
import { AgentTeamExecutionCard, runStatusLabel, useAgentTeamRunPolling } from "../agent-team/agent-team-execution-card";
import { readAgentTeamPreviewDisplay } from "../agent-team/agent-team-display";
import type { CallView } from "../call-view";
import { DetailSurface } from "./detail-parts";

/**
 * TeamRun 自身的状态：批准后调用当场完成，`completed` 只表示 Team 已开跑，Team 做得怎样只有这里知道
 * （结论另以汇报消息到达）。执行中与失败、驳回、停止都说；完成不说话。
 */
function TeamStatus({ id }: { id: number; }) {
  const query = useAgentTeamRunPolling(id);
  if (query.isError) return <span>状态同步失败</span>;
  const run = query.data;
  if (run?.status !== "running" && run?.status !== "failed" && run?.status !== "cancelled") return null;
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
    status: <TeamStatus id={preview.teamRunId} />,
    // 待批时展开审批表单；批准后就地展开执行卡（Team 是编排，不在后台看板上）。
    detail: {
      defaultOpen: context.artifact.awaitingUser,
      render: () => context.artifact.awaitingUser ? <AgentTeamApproval {...context} /> : <AgentTeamExecutionCard {...context} display={preview} />,
    },
  };
};
