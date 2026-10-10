"use client";

import { useEffect, useRef, useState } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";

import { AgentTeamConfigurationFields } from "@/components/agent-team-configuration-fields";
import { AgentTeamStatusEditor } from "@/components/agent-team-status-editor";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { removeNodesFromConfiguration, updateNodeInConfiguration } from "@/components/agent-team-graph-model";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfigurationDraft, AgentTeamNodeConfiguration, AgentTeamTransitionConfiguration } from "@/lib/api/agent-teams";

function nextNodeId(nodes: AgentTeamNodeConfiguration[]): string {
  let suffix = nodes.length + 1;
  while (nodes.some((node) => node.node_id === `node_${suffix}`)) suffix += 1;
  return `node_${suffix}`;
}

/** 表单模式负责结构化编辑；配置状态、验证与保存由外层编辑器统一管理。 */
export function AgentTeamConfigurationForm({
  configuration,
  profiles,
  scopeEditable,
  teamIdEditable,
  saving,
  onChange,
  onSave,
  onCancel,
}: {
  configuration: AgentTeamConfigurationDraft;
  profiles: AgentConfiguration[];
  scopeEditable: boolean;
  teamIdEditable: boolean;
  saving: boolean;
  onChange: (configuration: AgentTeamConfigurationDraft) => void;
  onSave: () => Promise<void>;
  onCancel: () => void;
}) {
  const [newlyAddedEntry, setNewlyAddedEntry] = useState<{
    kind: "node" | "transition";
    sequence: number;
  } | null>(null);
  const nodeListRef = useRef<HTMLElement | null>(null);
  const transitionListRef = useRef<HTMLElement | null>(null);
  const additionSequence = useRef(0);

  useEffect(() => {
    if (!newlyAddedEntry) return;
    const list = newlyAddedEntry.kind === "node" ? nodeListRef.current : transitionListRef.current;
    const entries = list?.querySelectorAll<HTMLElement>("[data-team-entry-card]");
    const entry = newlyAddedEntry.kind === "node" ? entries?.item(entries.length - 1) : entries?.item(0);
    if (!entry) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    entry.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
    const timeout = window.setTimeout(() => {
      setNewlyAddedEntry((current) => current?.sequence === newlyAddedEntry.sequence ? null : current);
    }, 1_450);
    return () => window.clearTimeout(timeout);
  }, [newlyAddedEntry]);

  const highlightNewEntry = (kind: "node" | "transition") => {
    additionSequence.current += 1;
    setNewlyAddedEntry({ kind, sequence: additionSequence.current });
  };

  const update = (changes: Partial<AgentTeamConfigurationDraft>) => onChange({ ...configuration, ...changes });

  const updateNode = (index: number, changes: Partial<AgentTeamNodeConfiguration>) => onChange(updateNodeInConfiguration(configuration, index, changes));

  const addNodeStatuses = (index: number, additions: string[]) => {
    const node = configuration.nodes[index];
    updateNode(index, { statuses: [...node.statuses, ...additions] });
  };

  const removeNodeStatus = (nodeIndex: number, statusIndex: number) => {
    const node = configuration.nodes[nodeIndex];
    if (!node) return;
    updateNode(nodeIndex, { statuses: node.statuses.filter((_, index) => index !== statusIndex) });
  };

  const addNode = () => {
    const nodeId = nextNodeId(configuration.nodes);
    const node: AgentTeamNodeConfiguration = {
      node_id: nodeId,
      name: `节点 ${configuration.nodes.length + 1}`,
      agent_id: profiles[0]?.agent_id ?? "",
      statuses: ["done"],
    };
    update({
      nodes: [...configuration.nodes, node],
      start_node_id: configuration.start_node_id || nodeId,
    });
    highlightNewEntry("node");
  };

  const removeNode = (nodeIndex: number) => onChange(removeNodesFromConfiguration(configuration, new Set([nodeIndex])));

  const updateTransition = (index: number, changes: Partial<AgentTeamTransitionConfiguration>) => {
    update({
      transitions: configuration.transitions.map((edge, edgeIndex) => edgeIndex === index ? { ...edge, ...changes } : edge),
    });
  };

  const addTransition = () => {
    const nextTransition = { from_node_id: "", status: "", target_node_id: "END" };
    update({
      transitions: [nextTransition, ...configuration.transitions],
    });
    highlightNewEntry("transition");
  };

  return (
    <div className="space-y-5 p-5">
      <AgentTeamConfigurationFields
        configuration={configuration}
        teamIdEditable={teamIdEditable}
        scopeEditable={scopeEditable}
        layout="form"
        onChange={(changes) => update(changes)}
      />
      <label className="block space-y-1.5 text-sm">
        <span className="text-muted-foreground">入口节点</span>
        <select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={configuration.start_node_id} onChange={(event) => update({ start_node_id: event.target.value })}>
          <option value="">选择入口节点</option>
          {configuration.nodes.map((node, index) => <option key={`start-${index}`} value={node.node_id}>{node.name || node.node_id} · {node.node_id} · {index + 1}</option>)}
        </select>
      </label>

      <section ref={nodeListRef} className="space-y-3">
        <div className="flex items-center justify-between"><h3 className="text-sm font-medium">Team 节点</h3><Button type="button" variant="outline" size="sm" onClick={addNode}><PlusIcon />添加节点</Button></div>
        {configuration.nodes.length === 0 && <p className="text-muted-foreground rounded-xl border border-dashed p-4 text-sm">添加至少一个节点，再设置入口与转移。</p>}
        {configuration.nodes.map((node, index) => <article key={`node-${index}`} data-team-entry-card className={`border-border/70 space-y-3 rounded-xl border p-3 ${index === 0 && newlyAddedEntry?.kind === "node" ? "agent-team-entry-highlight" : ""}`}>
          <div className="flex items-start gap-2"><label className="min-w-0 flex-1 space-y-1 text-xs"><span className="text-muted-foreground">节点 ID</span><Input value={node.node_id} onChange={(event) => updateNode(index, { node_id: event.target.value })} /></label><Button type="button" variant="ghost" size="icon-sm" className="mt-5 text-destructive" aria-label={`删除节点 ${node.name} ${index + 1}`} onClick={() => removeNode(index)}><Trash2Icon /></Button></div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-xs"><span className="text-muted-foreground">节点名称</span><Input value={node.name} onChange={(event) => updateNode(index, { name: event.target.value })} /></label>
            <label className="space-y-1 text-xs"><span className="text-muted-foreground">执行子 Agent</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={node.agent_id} onChange={(event) => updateNode(index, { agent_id: event.target.value })}><option value="">选择子 Agent</option>{profiles.map((profile) => <option key={profile.agent_id} value={profile.agent_id}>{profile.agent_id}</option>)}</select></label>
          </div>
          <div className="space-y-1 text-xs">
            <span className="text-muted-foreground">业务状态</span>
            <AgentTeamStatusEditor
              key={`node-statuses-${index}`}
              statuses={node.statuses}
              onAdd={(statuses) => addNodeStatuses(index, statuses)}
              onRemove={(statusIndex) => removeNodeStatus(index, statusIndex)}
            />
          </div>
        </article>)}
      </section>

      <section ref={transitionListRef} className="space-y-3">
        <div className="flex items-center justify-between"><h3 className="text-sm font-medium">状态转移</h3><Button type="button" variant="outline" size="sm" onClick={addTransition}><PlusIcon />添加转移</Button></div>
        {configuration.transitions.length === 0 && <p className="text-muted-foreground rounded-xl border border-dashed p-4 text-sm">尚无状态转移。可以在画布中拖动状态出口创建连线，或在这里添加转移草稿。</p>}
        {configuration.transitions.map((edge, index) => <div key={`transition-draft-${index}`} data-team-entry-card className={`grid items-end gap-2 rounded-xl border p-3 sm:grid-cols-[1fr_1fr_1fr_auto] ${index === 0 && newlyAddedEntry?.kind === "transition" ? "agent-team-entry-highlight" : ""}`}>
          <label className="space-y-1 text-xs"><span className="text-muted-foreground">来源节点</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={edge.from_node_id} onChange={(event) => updateTransition(index, { from_node_id: event.target.value })}><option value="">选择节点</option>{configuration.nodes.map((node, nodeIndex) => <option key={`source-${nodeIndex}`} value={node.node_id}>{node.name || node.node_id} · {node.node_id} · {nodeIndex + 1}</option>)}</select></label>
          <label className="space-y-1 text-xs"><span className="text-muted-foreground">触发状态</span><Input value={edge.status} onChange={(event) => updateTransition(index, { status: event.target.value })} /></label>
          <label className="space-y-1 text-xs"><span className="text-muted-foreground">目标节点</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={edge.target_node_id} onChange={(event) => updateTransition(index, { target_node_id: event.target.value })}><option value="">选择节点或 END</option><option value="END">END（结束 Team）</option>{configuration.nodes.map((node, nodeIndex) => <option key={`target-${nodeIndex}`} value={node.node_id}>{node.name || node.node_id} · {node.node_id} · {nodeIndex + 1}</option>)}</select></label>
          <Button type="button" variant="ghost" size="icon-sm" className="text-destructive" aria-label="删除转移" onClick={() => update({ transitions: configuration.transitions.filter((_, edgeIndex) => edgeIndex !== index) })}><Trash2Icon /></Button>
        </div>)}
      </section>

      {profiles.length === 0 && <p className="text-muted-foreground text-sm">当前作用域暂无子 Agent 配置，可先添加节点草稿并稍后选择 Agent。</p>}
      <div className="flex justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={onCancel}>取消</Button><Button type="button" onClick={() => void onSave()} disabled={saving}>{saving ? "保存中…" : "保存配置"}</Button></div>
    </div>
  );
}
