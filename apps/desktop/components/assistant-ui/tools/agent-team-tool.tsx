import { useEffect, useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CheckCircle2, UsersIcon } from "lucide-react";

import {
  confirmAgentTeamRun,
  getAgentTeamRun,
  rejectAgentTeamRun,
  type AgentTeamRun,
} from "@/lib/api/agent-teams";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { readToolArtifact } from "./types";
import { readAgentTeamPreviewDisplay } from "./agent-team-display";
import { ToolStatus } from "./tool-status";

type AgentTeamToolProps = ToolCallMessagePartProps;

type NodeResult = {
  node_id: string;
  task_id: number;
  run_id: number;
  completed: true;
  status: string;
  output: string;
};

const END_REASON_LABELS: Record<string, string> = {
  rejected_by_user: "方案已被用户驳回",
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

/** Agent Team 方案预览卡；确认时提交用户最终编辑后的配置，由后端重新校验。 */
export function AgentTeamTool({ artifact: rawArtifact }: AgentTeamToolProps) {
  const artifact = readToolArtifact(rawArtifact);
  const preview = readAgentTeamPreviewDisplay(artifact.display_data);
  const [state, setState] = useState<"idle" | "confirming" | "rejecting" | "confirmed" | "rejected" | "failed">("idle");
  const [message, setMessage] = useState("");
  const [teamRun, setTeamRun] = useState<AgentTeamRun | null>(null);
  const [goal, setGoal] = useState(preview?.goal ?? "");
  const [instructions, setInstructions] = useState<Record<string, string>>(preview?.instructions ?? {});
  const [feedback, setFeedback] = useState("");

  useEffect(() => {
    if (!preview) return;
    setGoal(preview.goal);
    setInstructions(preview.instructions);
  }, [preview?.teamRunId]);

  useEffect(() => {
    if (!preview) return undefined;
    let disposed = false;
    void getAgentTeamRun(preview.teamRunId)
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
          const rejected = current.end_reason === "rejected_by_user";
          setState(rejected ? "rejected" : "failed");
          setMessage(rejected ? "方案已驳回，主 Agent 将根据反馈重新生成" : current.status === "cancelled" ? "该 Agent Team 执行方案已取消" : "该 Agent Team 执行失败");
        }
      })
      .catch(() => {
        // TeamRun 不存在时保持原始方案展示，由确认动作暴露后端错误。
      });
    return () => {
      disposed = true;
    };
  }, [preview?.teamRunId]);

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

  if (!preview) return null;

  const confirm = async () => {
    setState("confirming");
    setMessage("");
    try {
      const result = await confirmAgentTeamRun(
        preview.teamRunId,
        preview.configuration,
        goal,
        instructions,
      );
      setTeamRun(result);
      setState("confirmed");
      setMessage(`已启动，当前节点：${result.active_node?.node_id ?? "入口"}`);
    } catch (error) {
      setState("failed");
      setMessage(error instanceof Error ? error.message : "确认失败，请让主 Agent 重新生成方案");
    }
  };
  const reject = async () => {
    const trimmedFeedback = feedback.trim();
    if (!trimmedFeedback) {
      setState("failed");
      setMessage("请填写驳回意见，主 Agent 才能据此重新生成方案");
      return;
    }
    setState("rejecting");
    setMessage("");
    try {
      const result = await rejectAgentTeamRun(preview.teamRunId, trimmedFeedback);
      setTeamRun(result);
      setState("rejected");
      setMessage("方案已驳回，主 Agent 正在根据意见重新生成");
    } catch (error) {
      setState("failed");
      setMessage(error instanceof Error ? error.message : "驳回失败");
    }
  };
  return (
    <div className="space-y-3 rounded-xl border border-border/70 bg-card p-3 shadow-sm">
      <div className="flex items-start gap-3">
        <UsersIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm">{preview.name}</span>
            <ToolStatus status={artifact.backendStatus} className="text-muted-foreground" />
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
              节点指令
              <Textarea
                aria-label={`${node.name} 的节点指令`}
                className="mt-1 min-h-14 resize-y bg-background text-xs"
                value={instructions[node.nodeId] ?? ""}
                disabled={state === "confirming" || state === "rejecting" || state === "confirmed" || state === "rejected"}
                onChange={(event) => setInstructions((current) => ({
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
