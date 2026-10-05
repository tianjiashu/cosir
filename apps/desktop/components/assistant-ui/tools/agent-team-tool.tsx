import { useEffect, useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CheckCircle2, UsersIcon } from "lucide-react";

import {
  confirmAgentTeamRun,
  getAgentTeamRun,
  getLatestAgentTeamRun,
  type AgentTeamRun,
} from "@/lib/api/agent-teams";
import { Button } from "@/components/ui/button";
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
  superseded_by_new_preview: "执行方案已被新的方案替代",
  team_start_failed: "Team 入口节点启动失败",
  transition_not_found: "节点状态没有匹配的转移规则",
  runtime_unavailable: "本地运行时不可用",
  next_node_start_failed: "下一节点启动失败",
  node_run_failed: "Team 节点执行失败",
  node_run_cancelled: "Team 节点执行被取消",
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
  const [state, setState] = useState<"idle" | "confirming" | "confirmed" | "failed">("idle");
  const [message, setMessage] = useState("");
  const [teamRun, setTeamRun] = useState<AgentTeamRun | null>(null);

  useEffect(() => {
    if (!preview) return undefined;
    let disposed = false;
    void getLatestAgentTeamRun(preview.parentTaskId, preview.parentRunId, preview.teamId)
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
  }, [preview?.parentTaskId, preview?.parentRunId, preview?.teamId]);

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
        preview.parentTaskId,
        preview.parentRunId,
        preview.teamId,
        preview.configuration,
      );
      setTeamRun(result);
      setState("confirmed");
      setMessage(`已启动，当前节点：${result.active_node?.node_id ?? "入口"}`);
    } catch (error) {
      setState("failed");
      setMessage(error instanceof Error ? error.message : "确认失败，请让主 Agent 重新生成方案");
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
          <p className="text-muted-foreground mt-1 text-xs">目标：{preview.goal}</p>
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
      ) : (
        <div className="flex items-center justify-between gap-2">
          <span className="text-muted-foreground text-xs">不满意时让主 Agent 重新调用 agent_team。</span>
          <Button type="button" size="sm" disabled={state === "confirming"} onClick={confirm}>
            {state === "confirming" ? "确认中…" : "确认执行"}
          </Button>
        </div>
      )}
      {state === "failed" && <p className="text-destructive text-xs">{message}</p>}
    </div>
  );
}
