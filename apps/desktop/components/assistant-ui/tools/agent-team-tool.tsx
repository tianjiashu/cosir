import { useEffect, useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CheckCircle2, ClockIcon, UsersIcon } from "lucide-react";

import { getAgentTeamRun, type AgentTeamRun } from "@/lib/api/agent-teams";
import { submitUserInputDecision } from "@/lib/assistant/submit-user-input-decision";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { readToolArtifact } from "./types";
import {
  readAgentTeamPreviewDisplay,
  readAgentTeamReviewRequest,
  readAgentTeamRunDisplay,
  type AgentTeamPreviewDisplay,
} from "./agent-team-display";
import { ToolStatus } from "./tool-status";

/** 渲染器额外收到所属 Run / Task：提交用户决定必须定位到具体 Run。 */
type AgentTeamToolProps = ToolCallMessagePartProps & {
  runId?: number | null;
  taskId?: number;
};

type NodeResult = {
  node_id: string;
  task_id: number;
  run_id: number;
  completed: true;
  status: string;
  output: string;
};

const END_REASON_LABELS: Record<string, string> = {
  superseded_by_new_preview: "执行方案已被新的方案替代",
  team_start_failed: "Team 入口节点启动失败",
  transition_not_found: "节点状态没有匹配的转移规则",
  runtime_unavailable: "本地运行时不可用",
  next_node_start_failed: "下一节点启动失败",
  node_run_failed: "Team 节点执行失败",
  node_run_cancelled: "Team 节点执行被取消",
  node_output_invalid: "Team 节点结构化结果无效",
  implicit_completion_missing: "节点未提交 Team 状态",
  cancelled: "Team 已取消",
  runtime_restarted: "后端重启后未自动恢复 Team",
};

function endReasonLabel(endReason: string): string {
  return END_REASON_LABELS[endReason] ?? "Agent Team 执行未完成";
}

function readNodeResults(state: Record<string, unknown>): NodeResult[] {
  const raw = state.node_executions;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is NodeResult => {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return false;
    const value = item as Record<string, unknown>;
    return typeof value.node_id === "string"
      && typeof value.task_id === "number"
      && typeof value.run_id === "number"
      && value.completed === true
      && typeof value.status === "string"
      && typeof value.output === "string";
  });
}

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
export function AgentTeamTool({ artifact: rawArtifact, runId, taskId }: AgentTeamToolProps) {
  const artifact = readToolArtifact(rawArtifact);
  const preview = readAgentTeamPreviewDisplay(artifact.display_data);
  const review = readAgentTeamReviewRequest(artifact.display_data);
  const runDisplay = readAgentTeamRunDisplay(artifact.display_data);
  // 待确认态用方案里的 TeamRun；已启动/恢复态用展示数据里的 TeamRun。两者共用同一套状态查询。
  const trackedRunId = preview?.teamRunId ?? runDisplay?.teamRunId;
  const [state, setState] = useState<"idle" | "confirming" | "rejecting" | "confirmed" | "rejected" | "failed">("idle");
  const [message, setMessage] = useState("");
  const [teamRun, setTeamRun] = useState<AgentTeamRun | null>(null);
  const [goal, setGoal] = useState(preview?.goal ?? "");
  const [nodeGoals, setNodeGoals] = useState<Record<string, string>>(preview?.nodeGoals ?? {});
  const [feedback, setFeedback] = useState("");
  // 「还在等用户作答」只取决于两件事：载荷里带着待决请求（后端挂起前投影的唯一标记），
  // 且本次会话内尚未提交决定。两者任一不成立就回到通用生命周期状态展示。
  const awaitingReview = review !== null && state !== "confirmed" && state !== "rejected";

  useEffect(() => {
    if (!preview) return;
    setGoal(preview.goal);
    setNodeGoals(preview.nodeGoals);
  }, [preview?.teamRunId]);

  useEffect(() => {
    if (trackedRunId === undefined) return undefined;
    let disposed = false;
    void getAgentTeamRun(trackedRunId)
      .then((current) => {
        if (disposed) return;
        if (current.status === "pending") {
          setTeamRun(null);
          setState("idle");
          setMessage("");
          return;
        }
        setTeamRun(current);
        if (current.status === "running") {
          setState("confirmed");
          setMessage(`已恢复，当前状态：${current.status}`);
          return;
        }
        if (current.status === "completed") {
          setState("confirmed");
          setMessage("已恢复，Team 已完成");
          return;
        }
        if (current.status === "failed" || current.status === "cancelled") {
          setState("failed");
          setMessage(current.status === "cancelled" ? "该 Agent Team 执行方案已取消" : "该 Agent Team 执行失败");
        }
      })
      .catch(() => {
        // TeamRun 不存在时保持原始方案展示，由确认动作暴露后端错误。
      });
    return () => {
      disposed = true;
    };
  }, [trackedRunId]);

  useEffect(() => {
    if (!teamRun || teamRun.status !== "running") return undefined;
    let disposed = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const current = await getAgentTeamRun(teamRun.id);
        if (disposed) return;
        setTeamRun(current);
        if (current.status === "running") {
          timer = window.setTimeout(() => void poll(), 1000);
        }
      } catch (error) {
        if (!disposed) setMessage(error instanceof Error ? error.message : "读取 Team 状态失败");
      }
    };
    timer = window.setTimeout(() => void poll(), 1000);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [teamRun]);

  if (!preview || !review) {
    // 没有可编辑方案时只剩运行态（用户已批准 / 恢复查看）：只展示 Team 进度。
    if (!runDisplay) return null;
    const currentStatus = teamRun?.status ?? runDisplay.status;
    return (
      <div className="space-y-2 rounded-xl border border-border/70 bg-card p-3 shadow-sm">
        <div className="flex items-center gap-2">
          <UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
          <span className="font-medium text-sm">{runDisplay.teamId}</span>
          <ToolStatus status={artifact.backendStatus} className="text-muted-foreground" />
        </div>
        {runDisplay.goal && <p className="text-muted-foreground text-xs">目标：{runDisplay.goal}</p>}
        <div className="text-xs">
          Team 状态：{currentStatus}
          {teamRun?.active_node ? ` · 当前节点：${teamRun.active_node.node_id}` : ""}
        </div>
        {teamRun && readNodeResults(teamRun.state).length > 0 && (
          <div className="space-y-1.5 border-t border-border/50 pt-2 text-xs">
            {readNodeResults(teamRun.state).map((result, index) => (
              <div key={`${result.node_id}-${index}`} className="rounded-md bg-muted/40 px-2 py-1.5">
                <div className="font-medium">{result.node_id} · {result.status}</div>
                <div className="text-muted-foreground line-clamp-3">{result.output || "（无输出）"}</div>
              </div>
            ))}
          </div>
        )}
        {teamRun?.end_reason && (
          <div className="text-destructive text-xs">{endReasonLabel(teamRun.end_reason)}</div>
        )}
        <p className="text-muted-foreground text-xs">该执行方案已提交用户决定，不再接受重复确认。</p>
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
        decisions: [{ request_id: review.requestId, decision, data }],
      });
      setState(decision === "approve" ? "confirmed" : "rejected");
      setMessage(
        decision === "approve"
          ? "已提交确认，Agent Team 正在启动"
          : "已提交审阅后的目标和驳回意见，主 Agent 正在重新生成方案",
      );
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
          <div key={node.nodeId} className="rounded-md bg-muted/40 px-2 py-1.5">
            <span className="font-medium">{index + 1}. {node.name}</span>
            <span className="text-muted-foreground"> · {node.nodeType}</span>
            <span className="text-muted-foreground"> · {node.agentId} · {node.role}</span>
            <div className="text-muted-foreground mt-0.5">模型：{node.effectiveModelName}</div>
            <div className="text-muted-foreground mt-0.5">工具：{node.effectiveTools.join("、") || "无"}</div>
            <div className="text-muted-foreground mt-0.5">
              状态：{node.statuses.join("、")}
            </div>
            <label className="text-muted-foreground mt-2 block">
              节点子目标（必填）
              <Textarea
                aria-label={`${node.name} 的节点子目标`}
                className="mt-1 min-h-14 resize-y bg-background text-xs"
                value={nodeGoals[node.nodeId] ?? ""}
                disabled={state === "confirming" || state === "rejecting" || state === "confirmed" || state === "rejected"}
                onChange={(event) => setNodeGoals((current) => ({
                  ...current,
                  [node.nodeId]: event.target.value,
                }))}
              />
            </label>
          </div>
        ))}
      </div>
      <div className="text-muted-foreground text-xs">
        {preview.nodes.length} 个节点 · {preview.edges.length} 条转移
      </div>
      {state === "confirmed" ? (
        <div className="space-y-1 text-xs">
          <div className="flex items-center gap-1.5 text-emerald-600"><CheckCircle2 className="size-3.5" />{message}</div>
          {teamRun && <div className="text-muted-foreground">
            Team 状态：{teamRun.status} · 当前节点：{teamRun.active_node?.node_id ?? "无"}
          </div>}
          {teamRun && readNodeResults(teamRun.state).length > 0 && (
            <div className="space-y-1.5 border-t border-border/50 pt-2">
              <div className="font-medium text-foreground">节点结果</div>
              {readNodeResults(teamRun.state).map((result, index) => (
                <div key={`${result.node_id}-${index}`} className="rounded-md bg-muted/40 px-2 py-1.5">
                  <div className="font-medium">{result.node_id} · {result.status}</div>
                  <div className="text-muted-foreground line-clamp-3">{result.output || "（无输出）"}</div>
                </div>
              ))}
            </div>
          )}
          {teamRun?.end_reason && (
            <div className="text-destructive">{endReasonLabel(teamRun.end_reason)}</div>
          )}
          {teamRun?.status === "completed" && <div className="font-medium text-emerald-600">Team 已完成。</div>}
          {teamRun?.status === "cancelled" && <div className="font-medium text-muted-foreground">Team 已取消。</div>}
        </div>
      ) : state === "rejected" ? (
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
