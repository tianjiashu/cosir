import { useEffect, useState } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CheckCircle2, UsersIcon } from "lucide-react";

import {
  confirmAgentTeamPreview,
  getAgentTeamRun,
  getLatestAgentTeamRunByParent,
  type AgentTeamRun,
} from "@/lib/api/agent-teams";
import { Button } from "@/components/ui/button";
import { readToolArtifact } from "./types";
import { readAgentTeamPreviewDisplay } from "./agent-team-display";
import { ToolStatus } from "./tool-status";

type AgentTeamToolProps = ToolCallMessagePartProps;

type NodeResult = {
  node_id: string;
  status: string;
  output: string;
};

function readNodeResults(state: Record<string, unknown>): NodeResult[] {
  const raw = state.previous_outputs;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is NodeResult => {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return false;
    const value = item as Record<string, unknown>;
    return typeof value.node_id === "string"
      && typeof value.status === "string"
      && typeof value.output === "string";
  });
}

/** Agent Team 方案预览卡；确认动作只提交后端保存的精确预览，不接受前端改写的图。 */
export function AgentTeamTool({ artifact: rawArtifact }: AgentTeamToolProps) {
  const artifact = readToolArtifact(rawArtifact);
  const preview = readAgentTeamPreviewDisplay(artifact.display_data);
  const [state, setState] = useState<"idle" | "confirming" | "confirmed" | "failed">("idle");
  const [message, setMessage] = useState("");
  const [teamRun, setTeamRun] = useState<AgentTeamRun | null>(null);

  useEffect(() => {
    if (!preview) return undefined;
    let disposed = false;
    void getLatestAgentTeamRunByParent(
      preview.parentTaskId,
      preview.parentRunId,
      preview.previewFingerprint,
    )
      .then((current) => {
        if (disposed || current === null) return;
        setTeamRun(current);
        setState("confirmed");
        setMessage(`已恢复，当前状态：${current.status}`);
      })
      .catch(() => {
        // 未确认的预览没有 TeamRun；这里保持确认按钮，不把正常的 404/空结果当成错误展示。
      });
    return () => {
      disposed = true;
    };
  }, [preview?.parentTaskId, preview?.parentRunId, preview?.previewFingerprint]);

  useEffect(() => {
    if (!teamRun || !["pending", "running"].includes(teamRun.status)) return undefined;
    let disposed = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const current = await getAgentTeamRun(teamRun.team_run_id);
        if (disposed) return;
        setTeamRun(current);
        if (["pending", "running"].includes(current.status)) {
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
      const result = await confirmAgentTeamPreview(
        preview.parentTaskId,
        preview.parentRunId,
        preview.previewFingerprint,
      );
      setTeamRun(result);
      setState("confirmed");
      setMessage(`已启动，当前节点：${result.current_node_id ?? "入口"}`);
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
            Team 状态：{teamRun.status} · 当前节点：{teamRun.current_node_id ?? "无"}
            {teamRun.current_node_status ? ` · ${teamRun.current_node_status}` : ""}
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
          {teamRun?.current_node_output && <div className="text-muted-foreground line-clamp-3">{teamRun.current_node_output}</div>}
          {teamRun?.failure_message && <div className="text-destructive">{teamRun.failure_message}</div>}
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
