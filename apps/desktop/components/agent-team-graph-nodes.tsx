"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Handle, NodeToolbar, Position, useInternalNode, useUpdateNodeInternals, useViewport, type Align, type NodeProps } from "@xyflow/react";
import { CheckCircle2Icon, CircleIcon, FlagIcon, PlusIcon, Trash2Icon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { statusHandleId, type TeamGraphNode } from "@/components/agent-team-graph-model";
import type { AgentTeamNodeConfiguration } from "@/lib/api/agent-teams";

type GraphNodeEditorContextValue = {
  selectedNodeId: string | null;
  endNodeCount: number;
  getCanvasBounds: () => DOMRect | null;
  isNodeIdAvailable: (nodeId: string, currentNodeId: string) => boolean;
  onDismissNode: () => void;
  onUpdateNode: (nodeId: string, changes: Partial<AgentTeamNodeConfiguration>) => void;
  onSetStartNode: (nodeId: string) => void;
  onDeleteNode: (nodeId: string) => void;
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
  const nodeId = data.kind === "agent" ? data.node.node_id : "";
  const [nodeIdDraft, setNodeIdDraft] = useState(nodeId);
  const [nodeIdError, setNodeIdError] = useState("");
  const [newStatus, setNewStatus] = useState("");
  const [newStatusError, setNewStatusError] = useState("");

  useEffect(() => {
    updateNodeInternals(id);
  }, [id, statuses, updateNodeInternals]);
  useEffect(() => {
    setNodeIdDraft(nodeId);
  }, [nodeId]);

  if (data.kind !== "agent") return null;

  const isSelected = editor.selectedNodeId === id;
  const updateNode = (changes: Partial<AgentTeamNodeConfiguration>) => editor.onUpdateNode(data.node.node_id, changes);
  const addStatus = () => {
    const status = newStatus.trim();
    if (!/^[a-z][a-z0-9_]{0,127}$/.test(status)) {
      setNewStatusError("状态需以小写英文字母开头，只能包含小写字母、数字和下划线，最多 128 位");
      return;
    }
    if (data.node.statuses.includes(status)) {
      setNewStatusError("该状态已存在");
      return;
    }
    updateNode({ statuses: [...data.node.statuses, status] });
    setNewStatus("");
    setNewStatusError("");
  };
  const commitNodeId = () => {
    if (nodeIdDraft === data.node.node_id) return;
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(nodeIdDraft) || nodeIdDraft === "END") {
      setNodeIdError("ID 需为 1–128 位字母、数字、下划线或连字符，且不能是 END");
      return;
    }
    if (!editor.isNodeIdAvailable(nodeIdDraft, data.node.node_id)) {
      setNodeIdError("该节点 ID 已被使用");
      return;
    }
    updateNode({ node_id: nodeIdDraft });
    setNodeIdError("");
  };

  return (
    <>
      <article className={`relative w-64 overflow-visible rounded-2xl border bg-card shadow-lg transition-shadow ${isSelected ? "border-primary ring-2 ring-primary/25" : data.isStart ? "border-primary/60 ring-2 ring-primary/15" : "border-border"}`}>
        <Handle type="target" position={Position.Left} id="input" className="!size-3 !border-2 !border-background !bg-primary" />
        <header className="flex items-center gap-2 rounded-t-2xl border-b px-3 py-2.5">
          {data.isStart
            ? <FlagIcon aria-label="开始节点" className="size-4 shrink-0 text-primary" />
            : <CircleIcon className="size-4 shrink-0 text-muted-foreground" />}
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-sm font-semibold">{data.node.name || data.node.node_id}</h3>
            <p className="text-muted-foreground truncate text-[11px]">{data.isStart ? "START · " : "STEP · "}{data.node.node_id}</p>
          </div>
          {data.isStart && <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">开始</span>}
        </header>
        <div className="space-y-2 px-3 py-2.5">
          <p className="text-muted-foreground truncate text-xs">{data.node.agent_id || "未选择子 Agent"}</p>
          <div className="space-y-1.5">
            {statuses.map((status) => {
              const connected = data.hasTransitionByStatus[status] ?? false;
              return (
                <div key={status} className="relative flex min-h-6 items-center justify-between gap-2 rounded-md bg-muted/40 px-2">
                  <span className="truncate font-mono text-[11px]">{status}</span>
                  <span className={connected ? "text-emerald-600" : "text-amber-600"}>
                    {connected ? <CheckCircle2Icon aria-label="已设置转移" className="size-3.5" /> : <span className="text-[10px]">未连线</span>}
                  </span>
                  <Handle type="source" position={Position.Right} id={statusHandleId(status)} className="!size-3 !border-2 !border-background !bg-primary" aria-label={`${status} 状态转移出口`} />
                </div>
              );
            })}
          </div>
        </div>
      </article>

      {isSelected && <AgentTeamNodeToolbar id={id}>
        <section className="nodrag nopan bg-card w-80 max-h-[min(70vh,560px)] overflow-y-auto rounded-2xl border shadow-2xl" aria-label="节点属性编辑器">
          <header className="flex items-center justify-between border-b px-4 py-3">
            <div><h3 className="text-sm font-semibold">编辑节点</h3><p className="text-muted-foreground text-[11px]">属性直接跟随节点显示</p></div>
            <div className="flex items-center gap-1">
              <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭节点属性" onClick={(event) => { event.stopPropagation(); editor.onDismissNode(); }}><XIcon /></Button>
              <Button type="button" variant="ghost" size="icon-sm" className="text-destructive" aria-label={`删除节点 ${data.node.name}`} onClick={() => editor.onDeleteNode(data.node.node_id)}><Trash2Icon /></Button>
            </div>
          </header>
          <div className="space-y-3 p-4">
            <label className="block space-y-1 text-xs"><span className="text-muted-foreground">节点名称</span><Input className="nodrag" value={data.node.name} onChange={(event) => updateNode({ name: event.target.value })} /></label>
            <label className="block space-y-1 text-xs"><span className="text-muted-foreground">节点 ID</span><Input className="nodrag" aria-invalid={Boolean(nodeIdError)} value={nodeIdDraft} onChange={(event) => { setNodeIdDraft(event.target.value); setNodeIdError(""); }} onBlur={commitNodeId} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} />{nodeIdError && <span role="alert" className="text-destructive">{nodeIdError}</span>}</label>
            <div className="space-y-2">
              <div className="flex items-center justify-between"><h4 className="text-xs font-medium">业务状态</h4><span className="text-muted-foreground text-[10px]">每个状态需有转移</span></div>
              {data.node.statuses.map((status) => <div key={status} className="flex items-center justify-between rounded-lg bg-muted/60 px-2.5 py-1.5"><code className="text-xs">{status}</code><Button type="button" variant="ghost" size="icon-xs" aria-label={`删除状态 ${status}`} disabled={data.node.statuses.length <= 1} onClick={() => updateNode({ statuses: data.node.statuses.filter((item) => item !== status) })}><Trash2Icon /></Button></div>)}
              <div>
                <div className="flex gap-2"><Input className="nodrag" aria-label="新增业务状态" aria-invalid={Boolean(newStatusError)} aria-describedby={newStatusError ? "new-status-error" : undefined} value={newStatus} onChange={(event) => { setNewStatus(event.target.value); setNewStatusError(""); }} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addStatus(); } }} placeholder="例如 needs_changes" /><Button type="button" variant="outline" size="icon" aria-label="添加业务状态" onClick={addStatus}><PlusIcon /></Button></div>
                {newStatusError && <p id="new-status-error" role="alert" className="text-destructive mt-1 text-[11px]">{newStatusError}</p>}
              </div>
            </div>
            <div className="flex justify-between border-t pt-3"><Button type="button" variant="outline" size="sm" onClick={() => editor.onSetStartNode(data.node.node_id)} disabled={data.isStart}>{data.isStart ? "当前为开始节点" : "设为开始节点"}</Button><span className="text-muted-foreground self-center text-[10px]">唯一入口</span></div>
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
      <div className={`border-emerald-600/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 relative flex h-14 min-w-36 items-center justify-center rounded-full border px-5 text-sm font-bold shadow-md ${isSelected ? "ring-2 ring-emerald-500/30" : ""}`}>
        <Handle type="target" position={Position.Left} id="input" className="!size-3 !border-2 !border-background !bg-emerald-600" />
        <CheckCircle2Icon className="mr-2 size-4" />END
      </div>
      <NodeToolbar isVisible={editor.selectedNodeId === id} position={Position.Top} offset={12} className="!z-30">
        <div className="nodrag nopan bg-card flex items-center gap-3 rounded-xl border px-3 py-2 shadow-xl">
          <span className="text-xs font-medium">结束出口</span>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭结束出口属性" onClick={(event) => { event.stopPropagation(); editor.onDismissNode(); }}><XIcon /></Button>
          <Button type="button" variant="ghost" size="sm" className="text-destructive" disabled={editor.endNodeCount <= 1} onClick={() => editor.onDeleteEndNode(data.endNodeId)}>删除</Button>
        </div>
      </NodeToolbar>
    </>
  );
}
