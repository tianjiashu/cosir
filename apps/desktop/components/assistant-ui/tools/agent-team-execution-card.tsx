import { useEffect, useMemo, useState } from "react";
import { MarkerType, type Node, type Edge } from "@xyflow/react";
import { ReactFlow } from "@xyflow/react";
import { BotIcon, UsersIcon } from "lucide-react";

import { getAgentTeamRun, type AgentTeamRun } from "@/lib/api/agent-teams";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { Button } from "@/components/ui/button";
import { readToolArtifact } from "./types";
import { readAgentTeamRunDisplay } from "./agent-team-display";
import { ToolStatus } from "./tool-status";
import { findAgentTeamStatus } from "@/lib/agent-team-status";
import type { ToolPartRendererProps } from "./tool-renderer-registry";
import type { AgentTeamRunDisplay } from "./agent-team-display";

type AgentTeamExecutionCardProps = ToolPartRendererProps & {
  displayOverride?: AgentTeamRunDisplay;
};

const STATUS_LABELS: Record<string, string> = {
  queued: "等待中",
  pending: "等待中",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  skipped: "未经过",
};

const END_REASON_LABELS: Record<string, string> = {
  user_rejected: "方案已驳回",
  superseded_by_new_preview: "方案已被新方案替代",
  team_start_failed: "Team 启动失败",
  transition_not_found: "没有匹配的节点转移",
  runtime_unavailable: "本地运行时不可用",
  next_node_start_failed: "下一节点启动失败",
  node_run_failed: "节点执行失败",
  node_run_cancelled: "节点执行已取消",
  node_output_invalid: "节点结果无效",
  implicit_completion_missing: "节点未提交 Team 状态",
  cancelled: "Team 已取消",
  runtime_restarted: "后端重启后已取消",
  max_runs_exceeded: "达到最大节点执行次数",
};

function statusStyle(status: string): { borderColor: string; background: string } {
  if (status === "running") return { borderColor: "hsl(var(--primary))", background: "hsl(var(--primary) / 0.08)" };
  if (status === "completed") return { borderColor: "hsl(142 60% 40%)", background: "hsl(142 60% 40% / 0.08)" };
  if (status === "failed" || status === "cancelled") return { borderColor: "hsl(var(--destructive))", background: "hsl(var(--destructive) / 0.06)" };
  return { borderColor: "hsl(var(--border))", background: "hsl(var(--card))" };
}

export function AgentTeamExecutionCard(props: AgentTeamExecutionCardProps) {
  const artifact = readToolArtifact(props.artifact);
  const display = props.displayOverride ?? readAgentTeamRunDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const [run, setRun] = useState<AgentTeamRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!display) return undefined;
    let disposed = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const current = await getAgentTeamRun(display.teamRunId);
        if (disposed) return;
        setRun(current);
        setError(null);
        if (current.status === "pending" || current.status === "running") {
          timer = window.setTimeout(() => void poll(), 1000);
        }
      } catch (cause) {
        if (!disposed) {
          setError(cause instanceof Error ? cause.message : "读取 Team 进度失败");
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

  const graph = useMemo(() => {
    if (!run) return { nodes: [] as Node[], edges: [] as Edge[] };
    const nodes: Node[] = run.nodes.map((node, index) => ({
      id: node.node_id,
      position: { x: (index % 3) * 230, y: Math.floor(index / 3) * 125 },
      data: {
        label: `${node.name}\n${STATUS_LABELS[node.status] ?? node.status} · ${node.agent_id}`,
      },
      style: {
        ...statusStyle(node.status),
        borderWidth: run.current_node_id === node.node_id ? 2 : 1,
        borderRadius: 12,
        width: 190,
        minHeight: 66,
        fontSize: 12,
        whiteSpace: "pre-line",
        cursor: node.task_id === null ? "default" : "pointer",
      },
    }));
    nodes.push({
      id: "END",
      position: { x: (run.nodes.length % 3) * 230, y: Math.floor(run.nodes.length / 3) * 125 },
      data: { label: "完成" },
      style: {
        border: "1px dashed hsl(var(--border))",
        borderRadius: 999,
        width: 100,
        textAlign: "center",
        fontSize: 12,
      },
    });
    const edgeCounts = new Map<string, number>();
    const edges: Edge[] = run.transitions.map((transition, index) => {
      const key = `${transition.from_node_id}:${transition.target_node_id}`;
      const occurrence = edgeCounts.get(key) ?? 0;
      edgeCounts.set(key, occurrence + 1);
      const visited = run.transition_history.some((item) => item.from_node_id === transition.from_node_id
        && item.status === transition.status && item.target_node_id === transition.target_node_id);
      const sourceNode = run.nodes.find((node) => node.node_id === transition.from_node_id);
      const statusColor = (sourceNode
        ? findAgentTeamStatus(sourceNode.statuses, transition.status)?.color
        : undefined) ?? "hsl(var(--muted-foreground) / 0.45)";
      return {
        id: `transition-${index}`,
        source: transition.from_node_id,
        target: transition.target_node_id,
        label: transition.target_node_id === "END" ? `${transition.status} → END` : transition.status,
        animated: run.current_node_id === transition.from_node_id,
        style: {
          stroke: statusColor,
          strokeWidth: visited ? 2 : 1,
          opacity: visited ? 1 : 0.55,
        },
        markerEnd: { type: MarkerType.ArrowClosed, color: statusColor },
        labelStyle: { fontSize: 10, fill: "hsl(var(--muted-foreground))" },
        type: "smoothstep",
      };
    });
    return { nodes, edges };
  }, [run]);

  if (!display) return null;
  const currentStatus = run ? STATUS_LABELS[run.status] ?? run.status : "读取进度中";
  const openAgent = (taskId: number, title: string, role: string) => {
    actions?.openAgent({ taskId, title, role });
  };
  const totalAgents = run?.nodes.reduce((count, node) => count + node.agent_count, 0) ?? 0;

  return (
    <section className="space-y-3 rounded-xl border border-border/70 bg-card p-3 shadow-sm" aria-label="Agent Team 执行进度">
      <header className="flex items-start gap-2">
        <UsersIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{display.teamId}</span>
            <ToolStatus status={artifact.backendStatus} className="text-muted-foreground" />
            <span className="text-xs text-muted-foreground">{currentStatus}</span>
          </div>
          {(display.goal || run?.goal) && (
            <p className="mt-1 text-xs text-muted-foreground">{display.goal || run?.goal}</p>
          )}
        </div>
      </header>

      {run && graph.nodes.length > 0 && (
        <div className="h-56 overflow-hidden rounded-lg border border-border/60 bg-muted/15" aria-label="Team 节点流水线">
          <ReactFlow
            nodes={graph.nodes}
            edges={graph.edges}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            proOptions={{ hideAttribution: true }}
            onNodeClick={(_, node) => {
              const taskId = run.nodes.find((item) => item.node_id === node.id)?.task_id;
              if (taskId !== null && taskId !== undefined) {
                const item = run.nodes.find((candidate) => candidate.node_id === node.id);
                openAgent(taskId, item?.name ?? node.id, item?.agent_id ?? "Team 节点 Agent");
              }
            }}
          />
        </div>
      )}

      {run && (
        <div className="space-y-2">
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <BotIcon className="size-3.5" aria-hidden="true" />
            {run.nodes.length} 个执行节点 · {totalAgents} 个 Agent
          </div>
          <div className="space-y-2">
            {run.nodes.map((node) => (
              <article key={node.node_id} className="space-y-2 rounded-lg border border-border/70 bg-muted/15 p-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-7 px-1.5 font-medium"
                    disabled={node.task_id === null}
                    onClick={() => node.task_id !== null && openAgent(node.task_id, node.name, node.agent_id)}
                  >
                    {node.name}
                  </Button>
                  <span className="rounded-full bg-background px-2 py-0.5 text-[11px] text-muted-foreground">
                    {STATUS_LABELS[node.status] ?? node.status}
                  </span>
                  <span className="text-[11px] text-muted-foreground">{node.conversation_run_count} 次运行</span>
                </div>
                <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                  <span className="font-medium text-foreground/80">负责 Agent</span>
                  <code className="rounded bg-background px-1.5 py-0.5">{node.agent_id}</code>
                </div>
                {node.goal && (
                  <p className="whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">{node.goal}</p>
                )}
                {node.child_agents.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5 border-t border-border/60 pt-2">
                    <span className="text-[11px] text-muted-foreground">委派子 Agent</span>
                    {node.child_agents.map((agent) => (
                      <Button
                        key={agent.task_id}
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-6 gap-1.5 px-2 text-[11px]"
                        onClick={() => openAgent(agent.task_id, agent.title, "子 Agent")}
                      >
                        {agent.title}
                        <span className="text-muted-foreground">{STATUS_LABELS[agent.status] ?? agent.status}</span>
                      </Button>
                    ))}
                  </div>
                )}
              </article>
            ))}
          </div>
        </div>
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}
      {run?.end_reason && <p className="text-xs text-muted-foreground">{END_REASON_LABELS[run.end_reason] ?? run.end_reason}</p>}
    </section>
  );
}
