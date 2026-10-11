import { useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { ClockIcon, UsersIcon } from "lucide-react";

import { submitUserInputDecision } from "@/lib/assistant/submit-user-input-decision";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { statusColorStyle } from "@/lib/agent-team-status";
import { ToolStatus } from "./tool-status";
import { AgentTeamExecutionCard } from "./agent-team-execution-card";
import { asRecord, readToolArtifact } from "./types";
import {
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
  nodeGoals: Record<string, string>,
): string | null {
  if (!goal.trim()) return "请填写 Agent Team 目标";
  const missingNodeGoals = nodes.filter((node) => !nodeGoals[node.nodeId]?.trim());
  if (missingNodeGoals.length > 0) {
    return `请填写所有节点的子目标：${missingNodeGoals.map((node) => node.name).join("、")}`;
  }
  return null;
}

/** 待确认态的卡片状态文案：它不是工具生命周期状态，只是「这张卡在等用户」的展示提示。 */
const AWAITING_REVIEW_LABEL = "等待确认";

function NodeStatusBadge({ status }: { status: AgentTeamPreviewDisplay["nodes"][number]["statuses"][number] }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium leading-none",
      )}
      style={statusColorStyle(status.color)}
    >
      <span className="size-1.5 rounded-full" style={{ backgroundColor: status.color }} aria-hidden="true" />
      {status.name}
    </span>
  );
}

/**
 * 待确认提示。
 *
 * 为什么不复用 ``ToolStatus``：该组件渲染的是**工具生命周期状态**（此时确实是 ``running``——
 * 调用尚未结算），而用户此刻看到的是「等我确认」。两者不是同一维度，因此这里显式给一个展示提示，
 * 不改动状态语义（后端状态机里没有、也不需要 ``waiting``）。
 */
function AwaitingReviewStatus({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs text-amber-600", className)}>
      <ClockIcon className="size-3.5" aria-hidden="true" />
      {AWAITING_REVIEW_LABEL}
    </span>
  );
}

/** Agent Team 方案预览卡；确认时把用户决定交回等待中的 Run，由后端在确认边界重新校验。 */
export function AgentTeamTool(props: AgentTeamToolProps) {
  const { artifact: rawArtifact, runId, runStatus, taskId, onAttachTransport } = props;
  const artifact = readToolArtifact(rawArtifact);
  const preview = readAgentTeamPreviewDisplay(artifact.display_data);
  const review = readAgentTeamReviewRequest(artifact.display_data);
  const [state, setState] = useState<"idle" | "confirming" | "rejecting" | "confirmed" | "rejected" | "failed">("idle");
  const [message, setMessage] = useState("");
  const [goal, setGoal] = useState(preview?.goal ?? "");
  const [nodeGoals, setNodeGoals] = useState<Record<string, string>>(preview?.nodeGoals ?? {});
  const [feedback, setFeedback] = useState("");
  // 审批动作同时受两个后端事实约束：Run 正在等待用户输入，且 tool part 带有待决请求。
  // tool part 自身的 completed/running 只描述调用执行阶段，不能单独授权或禁用审批。
  const isWaitingForInput = runStatus === "waiting_for_input";
  const awaitingReview = isWaitingForInput
    && review !== null
    && state !== "confirmed"
    && state !== "rejected";

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
  if (isWaitingForInput && review === null) {
    return (
      <div
        className="space-y-1 rounded-xl border border-amber-500/40 bg-amber-500/5 p-3 shadow-sm"
        role="status"
        aria-label="Agent Team 审批信息未恢复"
      >
        <div className="font-medium text-sm">审批内容暂不可用</div>
        <p className="text-muted-foreground text-xs">
          运行仍在等待你的决定，但审批请求载荷缺失。刷新对话以从本地运行记录恢复。
        </p>
      </div>
    );
  }
  if (!review || !isWaitingForInput) {
    const displayData = asRecord(artifact.display_data);
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
            {awaitingReview
              ? <AwaitingReviewStatus />
              : <ToolStatus status={artifact.backendStatus} className="text-muted-foreground" />}
          </div>
          <p className="text-muted-foreground mt-1 text-xs">目标</p>
          <Textarea
            aria-label="Agent Team 目标"
            className="mt-1 min-h-16 resize-y text-xs"
            value={goal}
            disabled={state === "confirming" || state === "rejecting" || state === "confirmed" || state === "rejected"}
            onChange={(event) => setGoal(event.target.value)}
          />
        </div>
      </div>
      <div className="space-y-1.5 text-xs">
        {preview.nodes.map((node, index) => (
          <section key={node.nodeId} className="space-y-2.5 rounded-lg border border-border/70 bg-muted/15 p-3">
            <div className="flex items-start gap-2.5">
              <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-semibold text-primary">
                {index + 1}
              </span>
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{node.name}</span>
                  <span className="rounded-full border border-border/70 bg-background px-2 py-0.5 text-[10px] text-muted-foreground">
                    {node.role}
                  </span>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md bg-background/70 px-2.5 py-2">
              <span className="shrink-0 text-[11px] font-medium text-muted-foreground">状态：</span>
              <div className="flex flex-wrap gap-1.5" aria-label={`${node.name} 的可返回状态`}>
                {node.statuses.map((status) => (
                  <NodeStatusBadge key={status.name} status={status} />
                ))}
              </div>
            </div>
            <label className="block text-muted-foreground">
              <span>节点子目标 <span className="text-destructive">*</span></span>
              <Textarea
                aria-label={`${node.name} 的节点子目标`}
                className="mt-1 min-h-16 resize-y bg-background text-xs"
                value={nodeGoals[node.nodeId] ?? ""}
                disabled={state === "confirming" || state === "rejecting" || state === "confirmed" || state === "rejected"}
                onChange={(event) => setNodeGoals((current) => ({
                  ...current,
                  [node.nodeId]: event.target.value,
                }))}
              />
            </label>
          </section>
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
              disabled={state === "confirming" || state === "rejecting"}
              onChange={(event) => setFeedback(event.target.value)}
            />
          </label>
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="outline" disabled={state === "confirming" || state === "rejecting"} onClick={() => void reject()}>
              {state === "rejecting" ? "正在驳回…" : "驳回并重新生成"}
            </Button>
            <Button type="button" size="sm" disabled={state === "confirming" || state === "rejecting"} onClick={() => void confirm()}>
              {state === "confirming" ? "确认中…" : "确认执行"}
            </Button>
          </div>
        </div>
      )}
      {state === "failed" && <p className="text-destructive text-xs">{message}</p>}
    </div>
  );
}
