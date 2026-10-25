import { useEffect, useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CheckCircle2Icon, ChevronDownIcon, CircleIcon, ClockIcon, UsersIcon } from "lucide-react";

import { submitTeamDecision } from "@/lib/assistant/task-commands";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { statusColorStyle } from "@/lib/agent-team-status";
import { AgentTeamExecutionCard, useAgentTeamRunPolling } from "./agent-team-execution-card";
import { readToolArtifact } from "./types";
import { readAgentTeamPreviewDisplay, type AgentTeamPreviewDisplay } from "./agent-team-display";

/** 渲染器额外收到所属 Task：提交决定按 Task 与 TeamRun 寻址。 */
type AgentTeamToolProps = ToolCallMessagePartProps & {
  taskId?: number;
};

function validateReviewedInputs(
  goal: string,
  nodes: AgentTeamPreviewDisplay["nodes"],
  nodeGoals: AgentTeamPreviewDisplay["nodeGoals"],
): string | null {
  if (!goal.trim()) return "请填写 Agent Team 目标";
  const missingNodeGoals = nodes.filter((node) => !nodeGoals[node.nodeId]?.goal.trim());
  if (missingNodeGoals.length > 0) {
    return `请填写所有节点的子目标：${missingNodeGoals.map((node) => node.name).join("、")}`;
  }
  const missingSummaries = nodes.filter((node) => !nodeGoals[node.nodeId]?.summary.trim());
  if (missingSummaries.length > 0) {
    return `请填写所有节点的摘要：${missingSummaries.map((node) => node.name).join("、")}`;
  }
  const longSummaries = nodes.filter((node) => Array.from(nodeGoals[node.nodeId]?.summary.trim() ?? "").length > 10);
  if (longSummaries.length > 0) {
    return `节点摘要不能超过 10 个字：${longSummaries.map((node) => node.name).join("、")}`;
  }
  return null;
}

/** 待确认态的卡片状态文案：它来自 TeamRun 的 `pending`，不是工具生命周期状态。 */
const AWAITING_REVIEW_LABEL = "等待确认";

function NodeStatusBadge({ status }: { status: AgentTeamPreviewDisplay["nodes"][number]["statuses"][number] }) {
  return (
    <Badge variant="outline" size="sm" className="text-foreground h-5 gap-1.5 px-2" style={statusColorStyle(status.color)}>
      <span className="size-1.5 rounded-full" style={{ backgroundColor: status.color }} aria-hidden="true" />
      {status.name}
    </Badge>
  );
}

/**
 * 待确认提示。
 *
 * 为什么不复用 ``ToolStatus``：工具调用早已闭合（``completed``），而用户此刻看到的是「等我确认」。
 * 两者不是同一维度，确认态只来自 TeamRun 的 ``pending``。
 */
function AwaitingReviewStatus({ label = AWAITING_REVIEW_LABEL, className }: { label?: string; className?: string }) {
  return (
    <span className={cn("text-warning inline-flex items-center gap-1.5 text-xs", className)}>
      <ClockIcon className="size-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}

/**
 * Agent Team 方案卡：按 TeamRun 状态渲染，不依附工具调用生命周期。
 *
 * `pending` 显示可编辑的审批表单；用户驳回（`cancelled` + `user_rejected`）显示驳回结果；其余状态
 * 交给执行卡。决定提交后后端确定性执行（批准即后台运行 Team），再以消息唤醒主 Agent。
 */
export function AgentTeamTool(props: AgentTeamToolProps) {
  const { artifact: rawArtifact, taskId, toolCallId } = props;
  const artifact = readToolArtifact(rawArtifact);
  const preview = readAgentTeamPreviewDisplay(artifact.display_data);
  const runQuery = useAgentTeamRunPolling(preview?.teamRunId);
  const teamRun = runQuery.data ?? null;
  const [state, setState] = useState<"idle" | "confirming" | "rejecting" | "failed">("idle");
  const [message, setMessage] = useState("");
  const [goal, setGoal] = useState(preview?.goal ?? "");
  const [nodeGoals, setNodeGoals] = useState<AgentTeamPreviewDisplay["nodeGoals"]>(preview?.nodeGoals ?? {});
  const [feedback, setFeedback] = useState("");
  const isPending = teamRun?.status === "pending";
  const formDisabled = !isPending || state === "confirming" || state === "rejecting";
  const reviewStatusLabel = isPending
    ? AWAITING_REVIEW_LABEL
    : runQuery.isError
      ? "无法读取方案状态"
      : "正在同步方案状态";
  const teamRunStatus = teamRun?.status ?? null;

  useEffect(() => {
    if (!preview || teamRunStatus === null) return;
    void frontendLog("INFO", "agent_team_card_status_observed", "Team 方案卡已读到 TeamRun 状态", {
      data: { taskId, toolCallId, teamRunId: preview.teamRunId, status: teamRunStatus },
    });
  }, [preview?.teamRunId, taskId, teamRunStatus, toolCallId]);

  if (!preview) return null;
  if (teamRun && teamRun.status === "cancelled" && teamRun.end_reason === "user_rejected") {
    return (
      <div className="space-y-2 rounded-xl border border-border/70 bg-card p-3 shadow-sm" aria-label="已驳回的 Agent Team 方案">
        <div className="flex items-center gap-2">
          <UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
          <span className="font-medium text-sm">{preview.name}</span>
          <span className="text-xs text-muted-foreground">已驳回</span>
        </div>
        <p className="text-muted-foreground text-xs">主 Agent 会根据审阅意见重新生成方案。</p>
      </div>
    );
  }
  if (teamRun && teamRun.status !== "pending") {
    return (
      <AgentTeamExecutionCard
        {...props}
        displayOverride={{ teamRunId: preview.teamRunId, teamId: preview.teamId, goal: teamRun.goal }}
      />
    );
  }

  /** 提交决定；后端执行后以消息唤醒主 Agent，失败只影响本卡片的提示状态。 */
  const submitDecision = async (
    decision: "approve" | "reject",
    data: Record<string, unknown>,
    phase: "confirming" | "rejecting",
  ) => {
    if (taskId === undefined) {
      setState("failed");
      setMessage("无法定位该执行方案所属的对话，请刷新页面后重试");
      return;
    }
    setState(phase);
    setMessage("");
    try {
      await submitTeamDecision(taskId, { teamRunId: preview.teamRunId, decision, data });
      setState("idle");
      // 主 Agent 被决定消息唤醒后的输出经 Task 长订阅到达，这里只刷新 TeamRun 状态。
      void runQuery.refetch();
    } catch (error) {
      void frontendLog("ERROR", "agent_team_decision_submit_failed", "提交 Team 审批决定失败", {
        data: { taskId, teamRunId: preview.teamRunId, decision },
        error,
      });
      setState("failed");
      setMessage(userErrorMessage(error, "提交失败，请重试"));
    }
  };
  const confirm = async () => {
    const validationMessage = validateReviewedInputs(goal, preview.nodes, nodeGoals);
    if (validationMessage) {
      setState("failed");
      setMessage(validationMessage);
      return;
    }
    await submitDecision(
      "approve",
      { configuration: preview.configuration, goal, node_goals: nodeGoals },
      "confirming",
    );
  };
  const reject = async () => {
    const validationMessage = validateReviewedInputs(goal, preview.nodes, nodeGoals);
    if (validationMessage) {
      setState("failed");
      setMessage(validationMessage);
      return;
    }
    const trimmedFeedback = feedback.trim();
    if (!trimmedFeedback) {
      setState("failed");
      setMessage("请填写驳回意见，主 Agent 才能据此重新生成方案");
      return;
    }
    await submitDecision(
      "reject",
      { feedback: trimmedFeedback, goal, node_goals: nodeGoals },
      "rejecting",
    );
  };
  return (
    <div className="space-y-3 rounded-xl border border-border/70 bg-card p-3 shadow-sm">
      <div className="flex items-start gap-3">
        <UsersIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm">{preview.name}</span>
            <AwaitingReviewStatus label={reviewStatusLabel} />
          </div>
          <p className="text-muted-foreground mt-1 text-xs">目标</p>
          <Textarea
            aria-label="Agent Team 目标"
            className="mt-1 min-h-16 resize-y text-xs"
            value={goal}
            disabled={formDisabled}
            onChange={(event) => setGoal(event.target.value)}
          />
        </div>
      </div>
      <div className="space-y-1.5 text-xs">
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium">节点分工</span>
          <span className="text-muted-foreground">
            {preview.nodes.filter((node) => nodeGoals[node.nodeId]?.summary.trim() && nodeGoals[node.nodeId]?.goal.trim()).length}/{preview.nodes.length} 已完善
          </span>
        </div>
        {preview.nodes.map((node, index) => (
          <details
            key={node.nodeId}
            className="group border-b border-border/60 last:border-b-0"
          >
            <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2.5 rounded py-2 focus-ring [&::-webkit-details-marker]:hidden">
              {nodeGoals[node.nodeId]?.summary.trim() && nodeGoals[node.nodeId]?.goal.trim()
                ? <CheckCircle2Icon className="size-4 shrink-0 text-success" aria-label="已完善" />
                : <CircleIcon className="size-4 shrink-0 text-muted-foreground" aria-label="待完善" />}
              <span className="flex size-5 shrink-0 items-center justify-center rounded bg-muted text-2xs font-semibold text-muted-foreground">
                {index + 1}
              </span>
              <span className="min-w-0 flex-1 truncate font-medium" title={node.name}>{node.name}</span>
              <span className="hidden max-w-40 truncate text-2xs text-muted-foreground sm:inline" title={node.role}>{node.role}</span>
              <span className="shrink-0 text-2xs text-muted-foreground">{node.statuses.length} 个状态</span>
              <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden="true" />
            </summary>
            <div className="space-y-3 pb-3 pl-7 pt-1">
              <div className="flex flex-wrap items-center gap-1.5" aria-label={`${node.name} 的可返回状态`}>
                <span className="mr-1 text-2xs font-medium text-muted-foreground">可返回状态</span>
                {node.statuses.map((status) => (
                  <NodeStatusBadge key={status.name} status={status} />
                ))}
              </div>
              <label className="block text-muted-foreground">
                <span>节点摘要 <span className="text-destructive">*</span><span className="ml-1 text-2xs">最多 10 字</span></span>
                <Input
                  aria-label={`${node.name} 的节点摘要`}
                  className="mt-1 bg-background text-xs"
                  maxLength={10}
                  placeholder="例如：撰写与修订总规划"
                  value={nodeGoals[node.nodeId]?.summary ?? ""}
                  disabled={formDisabled}
                  onChange={(event) => setNodeGoals((current) => ({
                    ...current,
                    [node.nodeId]: {
                      goal: current[node.nodeId]?.goal ?? "",
                      summary: event.target.value,
                    },
                  }))}
                />
              </label>
              <label className="block text-muted-foreground">
                <span>节点子目标 <span className="text-destructive">*</span></span>
                <Textarea
                  aria-label={`${node.name} 的节点子目标`}
                  className="mt-1 min-h-16 resize-y bg-background text-xs"
                  value={nodeGoals[node.nodeId]?.goal ?? ""}
                  disabled={formDisabled}
                  onChange={(event) => setNodeGoals((current) => ({
                    ...current,
                    [node.nodeId]: {
                      goal: event.target.value,
                      summary: current[node.nodeId]?.summary ?? "",
                    },
                  }))}
                />
              </label>
            </div>
          </details>
        ))}
      </div>
      <div className="text-muted-foreground text-xs">
        {preview.nodes.length} 个节点 · {preview.edges.length} 条转移
      </div>
      <div className="space-y-2">
          <label className="text-muted-foreground block text-xs">
            驳回意见
            <Textarea
              aria-label="驳回意见"
              className="mt-1 min-h-16 resize-y bg-background"
              placeholder="说明需要调整的目标、分工或执行方式…"
              value={feedback}
              disabled={formDisabled}
              onChange={(event) => setFeedback(event.target.value)}
            />
          </label>
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="outline" disabled={formDisabled} onClick={() => void reject()}>
              {state === "rejecting" ? "正在驳回…" : "驳回并重新生成"}
            </Button>
            <Button type="button" size="sm" disabled={formDisabled} onClick={() => void confirm()}>
              {state === "confirming" ? "确认中…" : "确认执行"}
            </Button>
        </div>
      </div>
      {message && <p className={cn("text-xs", state === "failed" ? "text-destructive" : "text-muted-foreground")}>{message}</p>}
    </div>
  );
}
