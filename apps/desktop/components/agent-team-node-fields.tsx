"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowRightIcon, FlagIcon, Trash2Icon } from "lucide-react";

import { AgentTeamStatusEditor } from "@/components/agent-team-status-editor";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamNodeConfiguration, AgentTeamTransitionConfiguration } from "@/lib/api/agent-teams";
import type { EditorNode, EditorTransition } from "@/lib/agent-team-editor/model";
import { END_NODE_ID } from "@/lib/agent-team-editor/model";
import { isImeComposing } from "@/lib/keyboard/ime";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

/**
 * Team 节点的编辑字段，画布属性面板与表单大纲共用同一份实现。
 *
 * 节点 ID 被转移与入口引用，改名由 `updateNodeInConfiguration` 连带改写引用，且仅在旧 ID
 * 唯一时改写；若逐字提交，中途与其他节点重名的那一刻会让转移挂到别的节点上，故只在失焦或
 * 回车时提交草稿。名称不被引用，逐字提交并由会话合并成一步撤销。
 */
export function TeamNodeFields({ node, ordinal, isStart, profiles, surface, onUpdate, onSetStart, onDelete }: {
  node: EditorNode;
  /** 从 1 开始的序号，区分同名节点的删除按钮。 */
  ordinal: number;
  isStart: boolean;
  profiles: AgentConfiguration[];
  /** 只用于日志，区分输入来自表单还是画布。 */
  surface: "form" | "canvas";
  onUpdate: (changes: Partial<AgentTeamNodeConfiguration>) => void;
  onSetStart: () => void;
  onDelete: () => void;
}) {
  const [nodeIdDraft, setNodeIdDraft] = useState(node.node_id);
  const activeFieldTraceRef = useRef<{ field: string; traceId: string; changed: boolean } | null>(null);

  useEffect(() => {
    setNodeIdDraft(node.node_id);
  }, [node.node_id]);

  // 字段级日志用于排查输入被画布拖拽或快捷键抢走焦点的问题。
  const onFieldFocus = (field: string) => {
    const traceId = newTraceId();
    activeFieldTraceRef.current = { field, traceId, changed: false };
    void frontendLog("INFO", "agent_team_node_field_focused", "节点属性输入框获得焦点", {
      traceId,
      data: { nodeId: node.node_id, field, surface },
    });
  };
  const onFieldChange = (field: string, valueLength: number) => {
    const activeField = activeFieldTraceRef.current;
    if (!activeField || activeField.field !== field || activeField.changed) return;
    activeField.changed = true;
    void frontendLog("INFO", "agent_team_node_field_changed", "节点属性输入框收到编辑内容", {
      traceId: activeField.traceId,
      data: { nodeId: node.node_id, field, surface, valueLength },
    });
  };
  const onFieldBlur = (field: string) => {
    const activeField = activeFieldTraceRef.current;
    if (!activeField || activeField.field !== field) return;
    activeFieldTraceRef.current = null;
    void frontendLog("INFO", "agent_team_node_field_blurred", "节点属性输入框失去焦点", {
      traceId: activeField.traceId,
      data: { nodeId: node.node_id, field, surface, changed: activeField.changed },
    });
  };
  const commitNodeId = () => {
    if (nodeIdDraft !== node.node_id) onUpdate({ node_id: nodeIdDraft });
  };

  return (
    <div className="@container space-y-4">
      <div className="grid gap-3 @lg:grid-cols-3">
        <label className="block space-y-1 text-xs"><span className="text-muted-foreground">节点名称</span><Input value={node.name} onFocus={() => onFieldFocus("name")} onChange={(event) => { onFieldChange("name", event.target.value.length); onUpdate({ name: event.target.value }); }} onBlur={() => onFieldBlur("name")} /></label>
        <label className="block space-y-1 text-xs"><span className="text-muted-foreground">节点 ID</span><Input value={nodeIdDraft} onFocus={() => onFieldFocus("node_id")} onChange={(event) => { onFieldChange("node_id", event.target.value.length); setNodeIdDraft(event.target.value); }} onBlur={() => { onFieldBlur("node_id"); commitNodeId(); }} onKeyDown={(event) => { if (event.key === "Enter" && !isImeComposing(event)) event.currentTarget.blur(); }} /></label>
        <label className="block space-y-1 text-xs"><span className="text-muted-foreground">执行子 Agent</span><NativeSelect value={node.agent_id} onChange={(event) => onUpdate({ agent_id: event.target.value })}><option value="">选择子 Agent</option>{profiles.map((item) => <option key={item.agent_id} value={item.agent_id}>{item.agent_id}</option>)}</NativeSelect></label>
      </div>
      <div className="space-y-1.5 text-xs">
        <span className="text-muted-foreground">业务状态</span>
        <AgentTeamStatusEditor
          statuses={node.statuses}
          onAdd={(statuses) => onUpdate({ statuses: [...node.statuses, ...statuses] })}
          onRemove={(statusIndex) => onUpdate({ statuses: node.statuses.filter((_, index) => index !== statusIndex) })}
          onColorChange={(statusIndex, color) => onUpdate({ statuses: node.statuses.map((status, index) => index === statusIndex ? { ...status, color } : status) })}
        />
        <p className="text-muted-foreground text-2xs">每个状态是节点的一个出口，按状态决定下一步交给谁。</p>
      </div>
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" className="flex-1 @lg:flex-none" onClick={onSetStart} disabled={isStart}><FlagIcon />{isStart ? "当前为入口" : "设为入口"}</Button>
        <Button type="button" variant="outline" size="sm" className="text-destructive flex-1 @lg:flex-none" aria-label={`删除节点 ${node.name} ${ordinal}`} onClick={onDelete}><Trash2Icon />删除节点</Button>
      </div>
    </div>
  );
}

/**
 * 一条状态转移的「状态 → 目标」编辑行；来源由所在位置决定（表单的节点卡片、画布选中的连线）。
 *
 * 状态只能从来源节点已声明的状态中选；转移引用了来源没有的状态（或来源节点不存在）时，
 * 保留当前值作为选项，交由后端校验，前端不静默改写。
 */
export function TeamTransitionFields({ transition, source, nodes, onChange, onDelete }: {
  transition: EditorTransition;
  source: EditorNode | undefined;
  nodes: EditorNode[];
  onChange: (changes: Partial<AgentTeamTransitionConfiguration>) => void;
  onDelete: () => void;
}) {
  const statusNames = source?.statuses.map((status) => status.name) ?? [];
  const statusOptions = statusNames.includes(transition.status) ? statusNames : [transition.status, ...statusNames];
  const knownTarget = transition.target_node_id === END_NODE_ID || nodes.some((node) => node.node_id === transition.target_node_id);
  const color = source?.statuses.find((status) => status.name === transition.status)?.color;

  return (
    <div className="flex items-center gap-2">
      <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: color ?? "var(--warning)" }} />
      <NativeSelect aria-label="触发状态" className="min-w-0 flex-1 font-mono text-xs" value={transition.status} onChange={(event) => onChange({ status: event.target.value })}>
        {statusOptions.map((name) => <option key={name} value={name}>{statusNames.includes(name) ? name || "（空状态）" : `${name || "（空状态）"}（来源未声明）`}</option>)}
      </NativeSelect>
      <ArrowRightIcon aria-hidden="true" className="text-muted-foreground size-3.5 shrink-0" />
      <NativeSelect aria-label="目标节点" className="min-w-0 flex-1 text-xs" value={transition.target_node_id} onChange={(event) => onChange({ target_node_id: event.target.value })}>
        {!knownTarget && <option value={transition.target_node_id}>{`未找到：${transition.target_node_id || "（空）"}`}</option>}
        <option value={END_NODE_ID}>END（结束 Team）</option>
        {nodes.map((node) => <option key={node.editorId} value={node.node_id}>{node.name || node.node_id}</option>)}
      </NativeSelect>
      <Button type="button" variant="ghost" size="icon-sm" className="text-destructive shrink-0" aria-label={`删除转移 ${transition.status || "空状态"}`} onClick={onDelete}><Trash2Icon /></Button>
    </div>
  );
}
