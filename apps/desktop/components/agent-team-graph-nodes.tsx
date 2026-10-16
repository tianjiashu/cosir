"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Handle, NodeToolbar, Position, useInternalNode, useUpdateNodeInternals, useViewport, type Align, type NodeProps } from "@xyflow/react";
import { CheckCircle2Icon, CircleIcon, FlagIcon, Trash2Icon, XIcon } from "lucide-react";

import { AgentTeamStatusEditor } from "@/components/agent-team-status-editor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { statusHandleId, type TeamGraphNode } from "@/components/agent-team-graph-model";
import type { AgentTeamNodeConfiguration } from "@/lib/api/agent-teams";
import { isImeComposing } from "@/lib/keyboard/ime";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

type GraphNodeEditorContextValue = {
  selectedNodeId: string | null;
  endNodeCount: number;
  getCanvasBounds: () => DOMRect | null;
  onDismissNode: () => void;
  onUpdateNode: (nodeIndex: number, changes: Partial<AgentTeamNodeConfiguration>) => void;
  onSetStartNode: (nodeId: string) => void;
  onDeleteNode: (nodeIndex: number) => void;
  onDeleteEndNode: (endNodeId: string) => void;
};

export const GraphNodeEditorContext = createContext<GraphNodeEditorContextValue | null>(null);

function useGraphNodeEditor() {
  const editor = useContext(GraphNodeEditorContext);
  if (!editor) throw new Error("画布节点必须位于图编辑器上下文中");
  return editor;
}

function AgentTeamNodeToolbar({ id, children }: { id: string; children: ReactNode }) {
  const editor = useGraphNodeEditor();
  const node = useInternalNode<TeamGraphNode>(id);
  const viewport = useViewport();
  const canvasBounds = editor.getCanvasBounds();
  const toolbarWidth = 320;
  const toolbarHeight = 560;
  const gap = 14;

  let position = Position.Bottom;
  let align: Align = "center";
  if (node && canvasBounds) {
    const { x, y } = node.internals.positionAbsolute;
    const zoom = viewport.zoom;
    const left = canvasBounds.left + viewport.x + x * zoom;
    const top = canvasBounds.top + viewport.y + y * zoom;
    const right = left + (node.measured.width ?? 256) * zoom;
    const bottom = top + (node.measured.height ?? 160) * zoom;
    const roomRight = canvasBounds.right - right;
    const roomLeft = left - canvasBounds.left;
    const roomBottom = canvasBounds.bottom - bottom;
    const roomTop = top - canvasBounds.top;

    if (roomRight >= toolbarWidth + gap) {
      position = Position.Right;
      align = roomTop < toolbarHeight / 2 ? "start" : roomBottom < toolbarHeight / 2 ? "end" : "center";
    } else if (roomLeft >= toolbarWidth + gap) {
      position = Position.Left;
      align = roomTop < toolbarHeight / 2 ? "start" : roomBottom < toolbarHeight / 2 ? "end" : "center";
    } else if (roomBottom >= toolbarHeight + gap) {
      position = Position.Bottom;
      align = roomLeft < toolbarWidth / 2 ? "start" : roomRight < toolbarWidth / 2 ? "end" : "center";
    } else if (roomTop >= toolbarHeight + gap) {
      position = Position.Top;
      align = roomLeft < toolbarWidth / 2 ? "start" : roomRight < toolbarWidth / 2 ? "end" : "center";
    } else {
      position = roomRight >= roomLeft ? Position.Right : Position.Left;
      align = roomTop < roomBottom ? "start" : "end";
    }
  }

  return <NodeToolbar isVisible={editor.selectedNodeId === id} position={position} align={align} offset={gap} className="!z-30">{children}</NodeToolbar>;
}

export function AgentTeamGraphNode({ id, data }: NodeProps<TeamGraphNode>) {
  const updateNodeInternals = useUpdateNodeInternals();
  const editor = useGraphNodeEditor();
  const statuses = data.kind === "agent" ? data.node.statuses : [];
  const orphanStatuses = data.kind === "agent" ? data.orphanStatuses : [];
  const nodeId = data.kind === "agent" ? data.node.node_id : "";
  const [nodeIdDraft, setNodeIdDraft] = useState(nodeId);
  const activeFieldTraceRef = useRef<{ field: string; traceId: string; changed: boolean } | null>(null);

  const onFieldFocus = (field: string) => {
    const traceId = newTraceId();
    activeFieldTraceRef.current = { field, traceId, changed: false };
    void frontendLog("INFO", "agent_team_graph_node_field_focused", "节点属性输入框获得焦点", {
      traceId,
      data: { nodeId, field },
    });
  };
  const onFieldChange = (field: string, valueLength: number) => {
    const activeField = activeFieldTraceRef.current;
    if (!activeField || activeField.field !== field || activeField.changed) return;
    activeField.changed = true;
    void frontendLog("INFO", "agent_team_graph_node_field_changed", "节点属性输入框收到编辑内容", {
      traceId: activeField.traceId,
      data: { nodeId, field, valueLength },
    });
  };
  const onFieldBlur = (field: string) => {
    const activeField = activeFieldTraceRef.current;
    if (!activeField || activeField.field !== field) return;
    activeFieldTraceRef.current = null;
    void frontendLog("INFO", "agent_team_graph_node_field_blurred", "节点属性输入框失去焦点", {
      traceId: activeField.traceId,
      data: { nodeId, field, changed: activeField.changed },
    });
  };

  useEffect(() => {
    updateNodeInternals(id);
  }, [id, statuses, orphanStatuses, updateNodeInternals]);
  useEffect(() => {
    setNodeIdDraft(nodeId);
  }, [nodeId]);

  if (data.kind !== "agent") return null;

  const isSelected = editor.selectedNodeId === id;
  const updateNode = (changes: Partial<AgentTeamNodeConfiguration>) => editor.onUpdateNode(data.nodeIndex, changes);
  const commitNodeId = () => {
    if (nodeIdDraft === data.node.node_id) return;
    updateNode({ node_id: nodeIdDraft });
  };

  return (
    <>
      <article className={`relative w-64 overflow-visible rounded-2xl border bg-card shadow-lg transition-shadow ${isSelected ? "border-info ring-2 ring-info/25" : data.isStart ? "border-primary/60 ring-2 ring-primary/15" : "border-border"}`}>
        <Handle type="target" position={Position.Left} id="input" aria-label="接收连线的输入端口" title="输入端口：接收其他节点的连线" className="!size-3 !border-2 !border-background !bg-info" />
        <header className="flex items-center gap-2 rounded-t-2xl border-b px-3 py-2.5">
          {data.isStart
            ? <FlagIcon aria-label="开始节点" className="size-4 shrink-0 text-primary" />
            : <CircleIcon className="size-4 shrink-0 text-muted-foreground" />}
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-sm font-semibold">{data.node.name || data.node.node_id}</h3>
          </div>
          {data.isStart && <Badge tone="primary" size="sm">开始</Badge>}
        </header>
        <div className="space-y-2 px-3 py-2.5">
          <div className="space-y-1.5">
            {statuses.map((status) => {
              return (
                <div key={status.editorId} className="relative flex min-h-6 items-center justify-between gap-2 rounded-md bg-muted/40 px-2">
                  <span className="truncate font-mono text-2xs">{status.name}</span>
                  <Handle type="source" position={Position.Right} id={status.editorId} style={{ backgroundColor: status.color }} className="!size-3 !border-2 !border-background" aria-label={`${status.name} 状态出口，拖出以创建连线`} title={`${status.name}：拖出连线`} />
                </div>
              );
            })}
            {orphanStatuses.map((status, orphanIndex) => {
              const duplicateIndex = statuses.filter((item) => item.name === status).length
                + orphanStatuses.slice(0, orphanIndex).filter((item) => item === status).length;
              return (
                <div key={`orphan:${status}:${orphanIndex}`} className="relative flex min-h-6 items-center justify-between gap-2 rounded-md border border-warning/30 bg-warning/10 px-2">
                  <span className="truncate font-mono text-2xs">{status || "（空状态）"}</span>
                  <Handle type="source" position={Position.Right} id={statusHandleId(status, duplicateIndex)} className="!size-3 !border-2 !border-background !bg-warning" aria-label={`${status || "空状态"} 转移草稿出口`} title={`${status || "空状态"}：现有转移草稿`} />
                </div>
              );
            })}
          </div>
        </div>
      </article>

      {isSelected && <AgentTeamNodeToolbar id={id}>
        <section className="nodrag nopan bg-card w-80 max-h-[min(70vh,560px)] overflow-y-auto rounded-2xl border shadow-2xl" aria-label="节点属性编辑器" onPointerDown={(event) => event.stopPropagation()} onMouseDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
          <header className="flex items-center justify-between border-b px-4 py-3">
            <div><h3 className="text-sm font-semibold">编辑节点</h3><p className="text-muted-foreground text-2xs">属性直接跟随节点显示</p></div>
            <div className="flex items-center gap-1">
              <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭节点属性" onClick={(event) => { event.stopPropagation(); editor.onDismissNode(); }}><XIcon /></Button>
              <Button type="button" variant="ghost" size="icon-sm" className="text-destructive" aria-label={`删除节点 ${data.node.name}`} onClick={() => editor.onDeleteNode(data.nodeIndex)}><Trash2Icon /></Button>
            </div>
          </header>
          <div className="space-y-3 p-4">
            <label className="block space-y-1 text-xs"><span className="text-muted-foreground">节点名称</span><Input className="nodrag" value={data.node.name} onFocus={() => onFieldFocus("name")} onChange={(event) => { onFieldChange("name", event.target.value.length); updateNode({ name: event.target.value }); }} onBlur={() => onFieldBlur("name")} /></label>
            <label className="block space-y-1 text-xs"><span className="text-muted-foreground">节点 ID</span><Input className="nodrag" value={nodeIdDraft} onFocus={() => onFieldFocus("node_id")} onChange={(event) => { onFieldChange("node_id", event.target.value.length); setNodeIdDraft(event.target.value); }} onBlur={() => { onFieldBlur("node_id"); commitNodeId(); }} onKeyDown={(event) => { if (event.key === "Enter" && !isImeComposing(event)) event.currentTarget.blur(); }} /></label>
            <div className="space-y-2">
              <div className="flex items-center justify-between"><h4 className="text-xs font-medium">业务状态</h4></div>
              <AgentTeamStatusEditor
                statuses={data.node.statuses}
                onAdd={(statuses) => updateNode({ statuses: [...data.node.statuses, ...statuses] })}
                onRemove={(statusIndex) => updateNode({ statuses: data.node.statuses.filter((_, index) => index !== statusIndex) })}
                onColorChange={(statusIndex, color) => updateNode({ statuses: data.node.statuses.map((status, index) => index === statusIndex ? { ...status, color } : status) })}
              />
            </div>
            <div className="flex justify-between border-t pt-3"><Button type="button" variant="outline" size="sm" onClick={() => editor.onSetStartNode(data.node.node_id)} disabled={data.isStart}>{data.isStart ? "当前为开始节点" : "设为开始节点"}</Button><span className="text-muted-foreground self-center text-2xs">唯一入口</span></div>
          </div>
        </section>
      </AgentTeamNodeToolbar>}
    </>
  );
}

export function AgentTeamGraphTerminalNode({ id, data }: NodeProps<TeamGraphNode>) {
  const editor = useGraphNodeEditor();
  if (data.kind !== "terminal") return null;
  const isSelected = editor.selectedNodeId === id;

  return (
    <>
      <div className={`relative flex h-14 min-w-36 items-center justify-center rounded-full border border-success/30 bg-success/10 px-5 text-sm font-bold text-success shadow-md ${isSelected ? "ring-2 ring-info/25" : ""}`}>
        <Handle type="target" position={Position.Left} id="input" aria-label="END 接收连线的输入端口" title="输入端口：接收其他节点的连线" className="!size-3 !border-2 !border-background !bg-info" />
        <CheckCircle2Icon className="mr-2 size-4" />END
      </div>
      <NodeToolbar isVisible={editor.selectedNodeId === id} position={Position.Top} offset={12} className="!z-30">
        <div className="nodrag nopan bg-card flex items-center gap-3 rounded-xl border px-3 py-2 shadow-xl" onPointerDown={(event) => event.stopPropagation()} onMouseDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
          <span className="text-xs font-medium">结束出口</span>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭结束出口属性" onClick={(event) => { event.stopPropagation(); editor.onDismissNode(); }}><XIcon /></Button>
          <Button type="button" variant="ghost" size="sm" className="text-destructive" disabled={editor.endNodeCount <= 1} onClick={() => editor.onDeleteEndNode(data.endNodeId)}>删除</Button>
        </div>
      </NodeToolbar>
    </>
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
