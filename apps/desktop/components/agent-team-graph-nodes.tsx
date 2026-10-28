"use client";

import { createContext, useContext, useEffect, type CSSProperties } from "react";
import { Handle, Position, useUpdateNodeInternals, type NodeProps } from "@xyflow/react";
import { AlertTriangleIcon, CheckCircle2Icon, PlayIcon } from "lucide-react";

import { statusHandleId, type TeamGraphNode } from "@/components/agent-team-graph-model";
import type { AgentTeamGraphIssue } from "@/lib/api/agent-teams";
import { cn } from "@/lib/utils";

type GraphNodeViewContextValue = {
  selectedNodeId: string | null;
  /** 按 agent_id 查子 Agent 的角色；Agent 不在当前作用域时返回 undefined。 */
  agentRole: (agentId: string) => string | undefined;
  /** 后端结构检查落在该节点上的问题。 */
  nodeIssues: (nodeId: string) => AgentTeamGraphIssue[];
};

/** 节点只负责展示与连线端口；编辑统一在右侧属性面板，故上下文只含展示所需信息。 */
export const GraphNodeViewContext = createContext<GraphNodeViewContextValue | null>(null);

function useGraphNodeView() {
  const view = useContext(GraphNodeViewContext);
  if (!view) throw new Error("画布节点必须位于图编辑器上下文中");
  return view;
}

/** 由 agent_id 稳定派生色相，同一个子 Agent 在面板、节点与工具栏中颜色一致。 */
function agentHue(agentId: string): number {
  let hash = 0;
  for (const char of agentId) hash = (hash * 31 + char.charCodeAt(0)) % 360;
  return hash;
}

export function AgentAvatar({ agentId, className }: { agentId: string; className?: string }) {
  const hue = agentHue(agentId);
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-xl text-xs font-semibold uppercase",
        agentId
          ? "bg-[oklch(0.93_0.05_var(--agent-hue))] text-[oklch(0.42_0.12_var(--agent-hue))] dark:bg-[oklch(0.32_0.06_var(--agent-hue))] dark:text-[oklch(0.86_0.08_var(--agent-hue))]"
          : "bg-muted text-muted-foreground",
        className,
      )}
      style={{ "--agent-hue": hue } as CSSProperties}
    >
      {agentId ? agentId.replace(/[^a-zA-Z0-9]/g, "").slice(0, 2) || agentId.slice(0, 1) : "?"}
    </span>
  );
}

export function AgentTeamGraphNode({ id, data }: NodeProps<TeamGraphNode>) {
  const updateNodeInternals = useUpdateNodeInternals();
  const view = useGraphNodeView();
  const statuses = data.kind === "agent" ? data.node.statuses : [];
  const orphanStatuses = data.kind === "agent" ? data.orphanStatuses : [];

  useEffect(() => {
    updateNodeInternals(id);
  }, [id, statuses, orphanStatuses, updateNodeInternals]);

  if (data.kind !== "agent") return null;

  const isSelected = view.selectedNodeId === id;
  const role = data.node.agent_id ? view.agentRole(data.node.agent_id) : undefined;
  const issues = view.nodeIssues(data.node.node_id);

  return (
    <article className={cn(
      "bg-card relative w-64 overflow-visible rounded-2xl border shadow-md transition-[box-shadow,border-color] hover:shadow-lg",
      isSelected ? "border-info ring-info/25 ring-4" : issues.length ? "border-warning/60" : data.isStart ? "border-primary/50" : "border-border",
    )}>
      {issues.length > 0 && (
        <span
          className="bg-warning text-warning-foreground absolute -top-2.5 -right-2.5 inline-flex h-5 min-w-5 items-center justify-center gap-0.5 rounded-full px-1.5 text-2xs font-semibold shadow-sm"
          title={issues.map((issue) => issue.message).join("\n")}
          aria-label={`${issues.length} 个结构问题`}
        >
          <AlertTriangleIcon aria-hidden="true" className="size-3" />{issues.length}
        </span>
      )}
      {data.isStart && (
        <span className="bg-primary text-primary-foreground absolute -top-2.5 left-4 inline-flex h-5 items-center gap-1 rounded-full px-2 text-2xs font-semibold tracking-wide shadow-sm">
          <PlayIcon aria-hidden="true" className="size-2.5 fill-current" />START
        </span>
      )}
      <Handle type="target" position={Position.Left} id="input" aria-label="接收连线的输入端口" title="输入端口：接收其他节点的连线" className="!size-3 !border-2 !border-background !bg-info" />
      <header className="flex items-center gap-3 px-3 pt-3.5 pb-2.5">
        <AgentAvatar agentId={data.node.agent_id} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold">{data.node.name || data.node.node_id}</h3>
          <p className={cn("truncate text-2xs", data.node.agent_id ? "text-muted-foreground" : "text-warning")}>
            {data.node.agent_id ? `${data.node.agent_id}${role ? ` · ${role}` : ""}` : "尚未分配子 Agent"}
          </p>
        </div>
      </header>
      {(statuses.length > 0 || orphanStatuses.length > 0) && <div className="space-y-1 border-t px-3 py-2">
        {statuses.map((status) => (
          <div key={status.editorId} className="relative flex h-6 items-center justify-end">
            <span className="truncate font-mono text-2xs">{status.name}</span>
            <Handle type="source" position={Position.Right} id={status.editorId} style={{ backgroundColor: status.color }} className="!-right-[13px] !size-3 !border-2 !border-background" aria-label={`${status.name} 状态出口，拖出以创建连线`} title={`${status.name}：拖出连线`} />
          </div>
        ))}
        {orphanStatuses.map((status, orphanIndex) => {
          const duplicateIndex = statuses.filter((item) => item.name === status).length
            + orphanStatuses.slice(0, orphanIndex).filter((item) => item === status).length;
          return (
            <div key={`orphan:${status}:${orphanIndex}`} className="border-warning/30 bg-warning/10 relative flex h-6 items-center justify-end gap-1.5 rounded-md border px-2">
              <span className="truncate font-mono text-2xs">{status || "（空状态）"}</span>
              <Handle type="source" position={Position.Right} id={statusHandleId(status, duplicateIndex)} className="!-right-[13px] !size-3 !border-2 !border-background !bg-warning" aria-label={`${status || "空状态"} 转移草稿出口`} title={`${status || "空状态"}：现有转移草稿`} />
            </div>
          );
        })}
      </div>}
    </article>
  );
}

export function AgentTeamGraphTerminalNode({ id, data }: NodeProps<TeamGraphNode>) {
  const view = useGraphNodeView();
  if (data.kind !== "terminal") return null;
  const isSelected = view.selectedNodeId === id;

  return (
    <div className={cn(
      "border-success/30 bg-success/10 text-success relative flex h-12 min-w-32 items-center justify-center rounded-full border px-5 text-sm font-bold tracking-wide shadow-sm",
      isSelected && "ring-info/25 ring-4",
    )}>
      <Handle type="target" position={Position.Left} id="input" aria-label="END 接收连线的输入端口" title="输入端口：接收其他节点的连线" className="!size-3 !border-2 !border-background !bg-info" />
      <CheckCircle2Icon className="mr-2 size-4" />END
    </div>
  );
}

export function AgentTeamGraphUnresolvedEndpoint({ data }: NodeProps<TeamGraphNode>) {
  if (data.kind !== "unresolved-transition-endpoint") return null;
  return (
    <div className="relative max-w-56 rounded-lg border border-dashed border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning shadow-sm">
      {data.side === "source"
        ? <Handle type="source" position={Position.Right} id={statusHandleId(data.status)} aria-label={`未解析来源状态 ${data.status || "空状态"}`} className="!size-3 !border-2 !border-background !bg-warning" />
        : <Handle type="target" position={Position.Left} id="input" aria-label="未解析目标输入端口" className="!size-3 !border-2 !border-background !bg-warning" />}
      <span>{data.label}</span>
    </div>
  );
}
