"use client";

import { Trash2Icon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { transitionKey } from "@/components/agent-team-graph-model";
import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";

export function AgentTeamGraphInspector({
  configuration,
  selectedTransitionKey,
  onChange,
  onDeleteTransition,
  onDismiss,
}: {
  configuration: AgentTeamConfiguration;
  selectedTransitionKey: string | null;
  onChange: (configuration: AgentTeamConfiguration) => void;
  onDeleteTransition: (key: string) => void;
  onDismiss: () => void;
}) {
  const transition = configuration.transitions.find(
    (item) => transitionKey(item.from_node_id, item.status) === selectedTransitionKey,
  );
  if (!transition || !selectedTransitionKey) return null;

  return (
    <aside aria-label="转移属性" className="bg-card/95 absolute right-4 top-4 z-20 w-72 rounded-2xl border shadow-xl backdrop-blur">
      <header className="flex items-center justify-between border-b px-4 py-3">
        <div><h3 className="text-sm font-semibold">状态转移</h3><p className="text-muted-foreground mt-0.5 text-[11px]">{transition.from_node_id} · {transition.status}</p></div>
        <div className="flex items-center gap-1">
          <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭转移属性" onClick={onDismiss}><XIcon /></Button>
          <Button type="button" variant="ghost" size="icon-sm" className="text-destructive" aria-label="删除状态转移" onClick={() => onDeleteTransition(selectedTransitionKey)}><Trash2Icon /></Button>
        </div>
      </header>
      <div className="space-y-2 p-4">
        <label className="block space-y-1 text-xs"><span className="text-muted-foreground">目标节点</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={transition.target_node_id} onChange={(event) => onChange({ ...configuration, transitions: configuration.transitions.map((item) => item === transition ? { ...item, target_node_id: event.target.value } : item) })}><option value="END">END · 结束 Team</option>{configuration.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{node.name || node.node_id}</option>)}</select></label>
        <p className="text-muted-foreground text-[10px]">拖动连线也可以更换目标；多个 END 落点共用同一个结束语义。</p>
      </div>
    </aside>
  );
}
