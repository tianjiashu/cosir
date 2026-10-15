import { useEffect, useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CheckCircle2Icon, ChevronDownIcon, CircleIcon, ClockIcon, UsersIcon } from "lucide-react";

import { submitUserInputDecision } from "@/lib/assistant/submit-user-input-decision";
import { frontendLog } from "@/lib/logging/frontend-log";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { statusColorStyle } from "@/lib/agent-team-status";
import { AgentTeamExecutionCard } from "./agent-team-execution-card";
import { asRecord, readToolArtifact } from "./types";
import {
  hasAgentTeamReviewRequest,
  readAgentTeamPreviewDisplay,
  readAgentTeamReviewRequest,
  type AgentTeamPreviewDisplay,
} from "./agent-team-display";

/** 渲染器额外收到所属 Run / Task：提交用户决定必须定位到具体 Run。 */
type AgentTeamToolProps = ToolCallMessagePartProps & {
  runId?: number | null;
  runStatus?: string | null;
  taskId?: number;
  onAttachTransport?: () => Promise<void>;
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

/** 待确认态的卡片状态文案：它不是工具生命周期状态，只是「这张卡在等用户」的展示提示。 */
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
 * 为什么不复用 ``ToolStatus``：该组件渲染的是**工具生命周期状态**（此时确实是 ``running``——
 * 调用尚未结算），而用户此刻看到的是「等我确认」。两者不是同一维度，因此这里显式给一个展示提示，
 * 不改动状态语义（后端状态机里没有、也不需要 ``waiting``）。
 */
function AwaitingReviewStatus({ label = AWAITING_REVIEW_LABEL, className }: { label?: string; className?: string }) {
  return (
    <span className={cn("text-warning inline-flex items-center gap-1.5 text-xs", className)}>
      <ClockIcon className="size-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}

/** Agent Team 方案预览卡；确认时把用户决定交回等待中的 Run，由后端在确认边界重新校验。 */
export function AgentTeamTool(props: AgentTeamToolProps) {
  const { artifact: rawArtifact, runId, runStatus, taskId, toolCallId, onAttachTransport } = props;
  const artifact = readToolArtifact(rawArtifact);
  const preview = readAgentTeamPreviewDisplay(artifact.display_data);
  const review = readAgentTeamReviewRequest(artifact.display_data);
  const [state, setState] = useState<"idle" | "confirming" | "rejecting" | "confirmed" | "rejected" | "failed">("idle");
  const [message, setMessage] = useState("");
  const [goal, setGoal] = useState(preview?.goal ?? "");
  const [nodeGoals, setNodeGoals] = useState<AgentTeamPreviewDisplay["nodeGoals"]>(preview?.nodeGoals ?? {});
  const [feedback, setFeedback] = useState("");
  // 请求存在决定审批卡是否展示；Run 状态只决定何时允许把决定提交给后端。
  const isWaitingForInput = runStatus === "waiting_for_input";
  const displayData = asRecord(artifact.display_data);
  const rawReviewRequest = asRecord(displayData.user_input_request);
  const reviewRequestPresent = hasAgentTeamReviewRequest(displayData);
  const displayDataKeys = Object.keys(displayData).sort().join(",");
  const reviewRequestKeys = Object.keys(rawReviewRequest).sort().join(",");
  const requestId = typeof rawReviewRequest.request_id === "string" ? rawReviewRequest.request_id : null;
  const requestKind = typeof rawReviewRequest.request_kind === "string" ? rawReviewRequest.request_kind : null;
  const draftSchema = typeof rawReviewRequest.draft_schema === "string" ? rawReviewRequest.draft_schema : null;
  const parserAcceptedRequest = review !== null;
  const formDisabled = !isWaitingForInput
    || state === "confirming"
    || state === "rejecting"
    || state === "confirmed"
    || state === "rejected";
  const reviewStatusLabel = isWaitingForInput
    ? AWAITING_REVIEW_LABEL
    : runStatus === "running" || runStatus === "pending"
      ? "正在准备确认"
      : runStatus === "completed" || runStatus === "failed" || runStatus === "cancelled"
        ? "运行已结束，审批不可提交"
        : "正在同步运行状态";

  useEffect(() => {
    if (!reviewRequestPresent) return;
    const logMessage = parserAcceptedRequest
      ? "前端已读到有效 Team 审批请求"
      : "前端收到无法识别的 Team 审批请求";
    void frontendLog(
      parserAcceptedRequest ? "INFO" : "WARNING",
      "agent_team_approval_payload_observed",
      logMessage,
      {
        data: {
          taskId,
          runId,
          toolCallId,
          runStatus,
          teamRunId: preview?.teamRunId ?? null,
          backendStatus: artifact.backendStatus,
          displayDataPresent: artifact.display_data !== null,
          displayDataKeys: displayDataKeys ? displayDataKeys.split(",") : [],
          requestObjectPresent: reviewRequestPresent,
          requestKeys: reviewRequestKeys ? reviewRequestKeys.split(",") : [],
          requestId,
          requestKind,
          draftSchema,
          parserAcceptedRequest,
        },
      },
    );
  }, [
    artifact.backendStatus,
    displayDataKeys,
    draftSchema,
    preview?.teamRunId,
    reviewRequestPresent,
    requestId,
    requestKind,
    parserAcceptedRequest,
    reviewRequestKeys,
    runId,
    toolCallId,
    runStatus,
    taskId,
  ]);

  if (state === "confirmed" && preview) {
    return (
      <AgentTeamExecutionCard
        {...props}
        displayOverride={{
          teamRunId: preview.teamRunId,
          teamId: preview.teamId,
          goal,
        }}
      />
    );
  }

  if (!preview) return null;
  if (reviewRequestPresent && review === null) {
    return (
      <Alert
        tone="warning"
        role="status"
        aria-label="Agent Team 审批请求无法识别"
        title="审批请求格式不兼容"
        className="rounded-xl p-3 shadow-sm"
      >
        <p className="text-xs">当前客户端无法识别这份 Team 审批请求，请刷新对话后重试。</p>
      </Alert>
    );
  }
  if (!review) {
    if (displayData.user_decision !== "reject") {
      // 审批决定已消费后，后端会移除待决请求；是否拒绝只由后端投影的决定记录判定。
      // 同意后从历史消息恢复时也走 canonical TeamRun 查询，避免把旧审批预览误显示为拒绝。
      return (
        <AgentTeamExecutionCard
          {...props}
          displayOverride={{ teamRunId: preview.teamRunId, teamId: preview.teamId, goal: "" }}
        />
      );
    }
    return (
      <div className="space-y-2 rounded-xl border border-border/70 bg-card p-3 shadow-sm" aria-label="已驳回的 Agent Team 方案">
        <div className="flex items-center gap-2">
          <UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
          <span className="font-medium text-sm">{preview.name}</span>
          <span className="text-xs text-muted-foreground">已驳回</span>
        </div>
        <p className="text-muted-foreground text-xs">主 Agent 正在根据审阅意见重新生成方案。</p>
      </div>
    );
  }

  /** 提交决定并把控制权交回等待中的 Run；失败只影响本卡片的提示状态。 */
  const submitDecision = async (
    decision: "approve" | "reject",
    data: Record<string, unknown>,
    phase: "confirming" | "rejecting",
  ) => {
    if (!isWaitingForInput) {
      setMessage("运行状态尚未就绪，请稍后重试");
      return;
    }
    if (runId === undefined || runId === null || taskId === undefined) {
      setState("failed");
      setMessage("无法定位该执行方案所属的运行，请刷新页面后重试");
      return;
    }
    setState(phase);
    setMessage("");
    try {
      await submitUserInputDecision({
        taskId,
        runId,
        decision: { request_id: review.requestId, decision, data },
      });
      setState(decision === "approve" ? "confirmed" : "rejected");
      setMessage(
        decision === "approve"
          ? "已提交确认，Agent Team 正在启动"
          : "已提交审阅后的目标和驳回意见，主 Agent 正在重新生成方案",
      );
      if (onAttachTransport) {
        void onAttachTransport().catch(() => {
          setMessage("决定已提交，但实时连接中断，请刷新对话查看最新结果");
        });
      }
    } catch (error) {
      setState("failed");
      setMessage(error instanceof Error ? error.message : "提交失败，请重试");
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
      {state === "rejected" ? (
        <p className="text-muted-foreground text-xs">{message}</p>
      ) : (
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
            {review.decisions.includes("reject") && (
              <Button type="button" size="sm" variant="outline" disabled={formDisabled} onClick={() => void reject()}>
                {state === "rejecting" ? "正在驳回…" : "驳回并重新生成"}
              </Button>
            )}
            {review.decisions.includes("approve") && (
              <Button type="button" size="sm" disabled={formDisabled} onClick={() => void confirm()}>
                {state === "confirming" ? "确认中…" : "确认执行"}
              </Button>
            )}
          </div>
        </div>
      )}
      {state === "failed" && <p className="text-destructive text-xs">{message}</p>}
    </div>
  );
}
