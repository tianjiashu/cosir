"use client";

import { useState } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfiguration, AgentTeamNodeConfiguration, AgentTeamTransitionConfiguration } from "@/lib/api/agent-teams";

type AgentProfilesByScope = Record<AgentTeamConfiguration["scope"], AgentConfiguration[]>;

const emptyConfiguration = (): AgentTeamConfiguration => ({
  team_id: "",
  name: "",
  description: "",
  max_runs: 10,
  start_node_id: "",
  nodes: [],
  transitions: [],
  scope: "workspace",
});

function parseStatuses(value: string): string[] {
  return [...new Set(value.split(",").map((status) => status.trim()).filter(Boolean))];
}

function nextNodeId(nodes: AgentTeamNodeConfiguration[]): string {
  let suffix = nodes.length + 1;
  while (nodes.some((node) => node.node_id === `node_${suffix}`)) suffix += 1;
  return `node_${suffix}`;
}

/** Team 节点和转移的结构化编辑表单；静态图校验仍由后端领域模型完成。 */
export function AgentTeamConfigurationEditor({
  initial,
  profilesByScope,
  scopeEditable,
  onChange,
  onCancel,
  onSave,
}: {
  initial: AgentTeamConfiguration | null;
  profilesByScope: AgentProfilesByScope;
  scopeEditable: boolean;
  onChange?: (configuration: AgentTeamConfiguration) => void;
  onCancel: () => void;
  onSave: (configuration: AgentTeamConfiguration) => Promise<void>;
}) {
  const [configuration, setConfiguration] = useState<AgentTeamConfiguration>(() => initial ? { ...initial } : emptyConfiguration());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const profiles = profilesByScope[configuration.scope].filter((profile) => profile.validation_status === "valid");

  const update = (changes: Partial<AgentTeamConfiguration>) => {
    const next = { ...configuration, ...changes };
    setConfiguration(next);
    onChange?.(next);
    setError(null);
  };

  const updateNode = (index: number, changes: Partial<AgentTeamNodeConfiguration>) => {
    const currentNode = configuration.nodes[index];
    const nextNodes = configuration.nodes.map((node, nodeIndex) => nodeIndex === index ? { ...node, ...changes } : node);
    const nextId = changes.node_id ?? currentNode.node_id;
    const nodeIdChanged = changes.node_id !== undefined && nextId !== currentNode.node_id;
    const transitions = nodeIdChanged
      ? configuration.transitions.map((transition) => ({
        ...transition,
        from_node_id: transition.from_node_id === currentNode.node_id ? nextId : transition.from_node_id,
        target_node_id: transition.target_node_id === currentNode.node_id ? nextId : transition.target_node_id,
      }))
      : configuration.transitions;
    update({
      nodes: nextNodes,
      transitions,
      start_node_id: configuration.start_node_id === currentNode.node_id && nextId ? nextId : configuration.start_node_id,
    });
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
    const node = configuration.nodes[0];
    if (!node) {
      setError("请先添加 Team 节点");
      return;
    }
    update({
      transitions: [...configuration.transitions, {
        from_node_id: node.node_id,
        status: node.statuses[0] ?? "done",
        target_node_id: "END",
      }],
    });
  };

  const save = async () => {
    if (!configuration.team_id.trim() || !/^[A-Za-z0-9_-]+$/.test(configuration.team_id)) {
      setError("Team ID 只能包含字母、数字、下划线和连字符");
      return;
    }
    if (!configuration.name.trim() || !configuration.description.trim()) {
      setError("请填写 Team 名称和用途说明");
      return;
    }
    if (!Number.isInteger(configuration.max_runs) || configuration.max_runs < 1) {
      setError("最大轮数必须是正整数");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave({ ...configuration, team_id: configuration.team_id.trim() });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="border-border/70 bg-card/80 rounded-2xl border shadow-sm">
      <header className="flex items-start justify-between gap-3 border-b px-5 py-4">
        <div><h2 className="font-medium">{initial ? "编辑 Agent Team" : "新建 Agent Team"}</h2><p className="text-muted-foreground mt-1 text-xs">节点按顺序执行；每个节点状态对应一条转移规则。</p></div>
        <Button type="button" variant="outline" onClick={onCancel}>返回 Team 列表</Button>
      </header>
      <div className="space-y-5 p-5">
        {error && <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}
        {scopeEditable && <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">保存范围</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={configuration.scope} onChange={(event) => update({ scope: event.target.value as AgentTeamConfiguration["scope"] })}><option value="workspace">当前工作区</option><option value="system">系统级</option></select></label>}
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">Team ID</span><Input value={configuration.team_id} disabled={Boolean(initial)} onChange={(event) => update({ team_id: event.target.value })} placeholder="例如 code_review_team" /></label>
          <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">Team 名称</span><Input value={configuration.name} onChange={(event) => update({ name: event.target.value })} /></label>
        </div>
        <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">用途说明</span><Input value={configuration.description} onChange={(event) => update({ description: event.target.value })} /></label>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">最大轮数</span><Input type="number" min={1} value={configuration.max_runs} onChange={(event) => update({ max_runs: Number(event.target.value) })} /></label>
          <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">入口节点</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={configuration.start_node_id} onChange={(event) => update({ start_node_id: event.target.value })}><option value="">选择入口节点</option>{configuration.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{node.name || node.node_id}</option>)}</select></label>
        </div>

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
        <div className="flex justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={onCancel}>取消</Button><Button type="button" onClick={() => void save()} disabled={saving}>{saving ? "保存中…" : "保存配置"}</Button></div>
      </div>
    </section>
  );
}
