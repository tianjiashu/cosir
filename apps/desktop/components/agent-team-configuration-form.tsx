"use client";

import { PlusIcon, Trash2Icon } from "lucide-react";

import { AgentTeamConfigurationFields } from "@/components/agent-team-configuration-fields";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { transitionKey, updateNodeInConfiguration, upsertTransition } from "@/components/agent-team-graph-model";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfiguration, AgentTeamNodeConfiguration, AgentTeamTransitionConfiguration } from "@/lib/api/agent-teams";

function parseStatuses(value: string): string[] {
  return [...new Set(value.split(",").map((status) => status.trim()).filter(Boolean))];
}

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
  error,
  saving,
  onChange,
  onError,
  onSave,
  onCancel,
}: {
  configuration: AgentTeamConfiguration;
  profiles: AgentConfiguration[];
  scopeEditable: boolean;
  teamIdEditable: boolean;
  error: string | null;
  saving: boolean;
  onChange: (configuration: AgentTeamConfiguration) => void;
  onError: (message: string) => void;
  onSave: () => Promise<void>;
  onCancel: () => void;
}) {
  const update = (changes: Partial<AgentTeamConfiguration>) => onChange({ ...configuration, ...changes });

  const updateNode = (index: number, changes: Partial<AgentTeamNodeConfiguration>) => {
    const currentNode = configuration.nodes[index];
    onChange(updateNodeInConfiguration(configuration, currentNode.node_id, changes));
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
  };

  const removeNode = (nodeId: string) => {
    const nodes = configuration.nodes.filter((node) => node.node_id !== nodeId);
    update({
      nodes,
      transitions: configuration.transitions.filter((edge) => edge.from_node_id !== nodeId && edge.target_node_id !== nodeId),
      start_node_id: configuration.start_node_id === nodeId ? nodes[0]?.node_id ?? "" : configuration.start_node_id,
    });
  };

  const updateTransition = (index: number, changes: Partial<AgentTeamTransitionConfiguration>) => {
    update({
      transitions: configuration.transitions.map((edge, edgeIndex) => edgeIndex === index ? { ...edge, ...changes } : edge),
    });
  };

  const addTransition = () => {
    const availableRoute = configuration.nodes.flatMap((node) => node.statuses.map((status) => ({ node, status })))
      .find(({ node, status }) => !configuration.transitions.some(
        (transition) => transitionKey(transition.from_node_id, transition.status) === transitionKey(node.node_id, status),
      ));
    if (!availableRoute) {
      onError(configuration.nodes.length === 0
        ? "请先添加 Team 节点"
        : "所有节点状态都已设置转移，请先添加状态");
      return;
    }
    const { node, status } = availableRoute;
    // 来源节点与状态共同标识一条转移；表单与画布共享此唯一性规则。
    update({
      transitions: upsertTransition(configuration.transitions, {
        from_node_id: node.node_id,
        status,
        target_node_id: "END",
      }),
    });
  };

  return (
    <div className="space-y-5 p-5">
      {error && <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}
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
          {configuration.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{node.name || node.node_id}</option>)}
        </select>
      </label>

      <section className="space-y-3">
        <div className="flex items-center justify-between"><h3 className="text-sm font-medium">Team 节点</h3><Button type="button" variant="outline" size="sm" onClick={addNode}><PlusIcon />添加节点</Button></div>
        {configuration.nodes.length === 0 && <p className="text-muted-foreground rounded-xl border border-dashed p-4 text-sm">添加至少一个节点，再设置入口与转移。</p>}
        {configuration.nodes.map((node, index) => <article key={`${index}-${node.node_id}`} className="border-border/70 space-y-3 rounded-xl border p-3">
          <div className="flex items-start gap-2"><label className="min-w-0 flex-1 space-y-1 text-xs"><span className="text-muted-foreground">节点 ID</span><Input value={node.node_id} onChange={(event) => updateNode(index, { node_id: event.target.value })} /></label><Button type="button" variant="ghost" size="icon-sm" className="mt-5 text-destructive" aria-label={`删除节点 ${node.name}`} onClick={() => removeNode(node.node_id)}><Trash2Icon /></Button></div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-xs"><span className="text-muted-foreground">节点名称</span><Input value={node.name} onChange={(event) => updateNode(index, { name: event.target.value })} /></label>
            <label className="space-y-1 text-xs"><span className="text-muted-foreground">执行子 Agent</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={node.agent_id} onChange={(event) => updateNode(index, { agent_id: event.target.value })}><option value="">选择子 Agent</option>{profiles.map((profile) => <option key={profile.agent_id} value={profile.agent_id}>{profile.agent_id} · {profile.role}</option>)}</select></label>
          </div>
          <label className="block space-y-1 text-xs"><span className="text-muted-foreground">业务状态（逗号分隔）</span><Input value={node.statuses.join(", ")} onChange={(event) => updateNode(index, { statuses: parseStatuses(event.target.value) })} placeholder="done, needs_changes" /></label>
        </article>)}
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between"><h3 className="text-sm font-medium">状态转移</h3><Button type="button" variant="outline" size="sm" onClick={addTransition}><PlusIcon />添加转移</Button></div>
        {configuration.transitions.length === 0 && <p className="text-muted-foreground rounded-xl border border-dashed p-4 text-sm">每个节点状态都需要一条转移；结束目标填写 END。</p>}
        {configuration.transitions.map((edge, index) => <div key={`${index}-${edge.from_node_id}-${edge.status}`} className="grid items-end gap-2 rounded-xl border p-3 sm:grid-cols-[1fr_1fr_1fr_auto]">
          <label className="space-y-1 text-xs"><span className="text-muted-foreground">来源节点</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={edge.from_node_id} onChange={(event) => updateTransition(index, { from_node_id: event.target.value })}><option value="">选择节点</option>{configuration.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{node.name || node.node_id}</option>)}</select></label>
          <label className="space-y-1 text-xs"><span className="text-muted-foreground">触发状态</span><Input value={edge.status} onChange={(event) => updateTransition(index, { status: event.target.value })} /></label>
          <label className="space-y-1 text-xs"><span className="text-muted-foreground">目标节点或 END</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={edge.target_node_id} onChange={(event) => updateTransition(index, { target_node_id: event.target.value })}><option value="END">END（结束 Team）</option>{configuration.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{node.name || node.node_id}</option>)}</select></label>
          <Button type="button" variant="ghost" size="icon-sm" className="text-destructive" aria-label="删除转移" onClick={() => update({ transitions: configuration.transitions.filter((_, edgeIndex) => edgeIndex !== index) })}><Trash2Icon /></Button>
        </div>)}
      </section>

      {profiles.length === 0 && <p className="text-destructive text-sm">当前作用域没有可用的子 Agent。请先配置子 Agent，或调整保存范围。</p>}
      <div className="flex justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={onCancel}>取消</Button><Button type="button" onClick={() => void onSave()} disabled={saving}>{saving ? "保存中…" : "保存配置"}</Button></div>
    </div>
  );
}
