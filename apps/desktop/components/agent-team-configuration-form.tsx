"use client";

import { useStore } from "zustand";
import type { TeamEditorSession } from "@/lib/agent-team-editor/session";
import { useEffect, useMemo, useRef, useState } from "react";
import { PlayIcon, PlusIcon, SaveIcon, UnplugIcon } from "lucide-react";

import { AgentTeamConfigurationFields } from "@/components/agent-team-configuration-fields";
import { TeamGraphIssueList } from "@/components/agent-team-graph-issues";
import { AgentAvatar } from "@/components/agent-team-graph-nodes";
import { TeamNodeFields, TeamTransitionFields } from "@/components/agent-team-node-fields";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/state-panel";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { EditorDraft, EditorNode } from "@/lib/agent-team-editor/model";
import type { TeamGraphIssues } from "@/lib/agent-team-editor/use-graph-issues";

type TeamOutline = {
  /** 从入口沿转移可达的节点下标，按广度优先的执行顺序排列。 */
  connected: number[];
  /** 入口出发走不到的节点下标，保持原有顺序。 */
  unconnected: number[];
  /** 来源节点不存在或 ID 重复、无法挂到某张节点卡片下的转移下标。 */
  dangling: number[];
};

/**
 * 把图结构排成大纲：只按转移关系排序与分组，帮助阅读执行流程。
 * 不复刻后端图校验（可达性、终止性等仍以保存时的后端结果为准）。
 */
function buildOutline(configuration: EditorDraft): TeamOutline {
  const uniqueIndex = new Map<string, number>();
  const duplicated = new Set<string>();
  configuration.nodes.forEach((node, index) => {
    if (uniqueIndex.has(node.node_id)) duplicated.add(node.node_id);
    else uniqueIndex.set(node.node_id, index);
  });
  for (const id of duplicated) uniqueIndex.delete(id);

  const connected: number[] = [];
  const visited = new Set<number>();
  const start = uniqueIndex.get(configuration.start_node_id);
  const queue = start === undefined ? [] : [start];
  if (start !== undefined) visited.add(start);
  while (queue.length) {
    const index = queue.shift()!;
    connected.push(index);
    for (const transition of configuration.transitions) {
      if (transition.from_node_id !== configuration.nodes[index].node_id) continue;
      const target = uniqueIndex.get(transition.target_node_id);
      if (target === undefined || visited.has(target)) continue;
      visited.add(target);
      queue.push(target);
    }
  }
  return {
    connected,
    unconnected: configuration.nodes.map((_, index) => index).filter((index) => !visited.has(index)),
    dangling: configuration.transitions.map((_, index) => index)
      .filter((index) => !uniqueIndex.has(configuration.transitions[index].from_node_id)),
  };
}

/**
 * 表单模式的「大纲视图」：节点按执行顺序排成卡片，每张卡片内编辑节点字段与它发出的转移。
 *
 * 节点与转移字段和画布属性面板共用 `TeamNodeFields` / `TeamTransitionFields`；配置状态、
 * 撤销历史与保存由外层编辑会话统一管理，本组件只发起编辑意图。
 */
export function AgentTeamConfigurationForm({
  session,
  profiles,
  scopeEditable,
  teamIdEditable,
  saving,
  graphIssues,
  onSave,
  onCancel,
}: {
  session: TeamEditorSession;
  profiles: AgentConfiguration[];
  scopeEditable: boolean;
  teamIdEditable: boolean;
  saving: boolean;
  graphIssues: TeamGraphIssues;
  onSave: () => void;
  onCancel: () => void;
}) {
  const configuration = useStore(session.store, state => state.snapshot.configuration);
  const outline = useMemo(() => buildOutline(configuration), [configuration]);
  const [highlightSequence, setHighlightSequence] = useState<number | null>(null);
  const nodeListRef = useRef<HTMLElement | null>(null);
  const additionSequence = useRef(0);

  // 新节点总在大纲末尾（首个节点成为入口，其余在「未连接」组），滚动到最后一张卡片并短暂高亮。
  useEffect(() => {
    if (highlightSequence === null) return;
    const entries = nodeListRef.current?.querySelectorAll<HTMLElement>("[data-team-entry-card]");
    const entry = entries?.item(entries.length - 1);
    if (!entry) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    entry.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
    const timeout = window.setTimeout(() => {
      setHighlightSequence((current) => current === highlightSequence ? null : current);
    }, 1_450);
    return () => window.clearTimeout(timeout);
  }, [highlightSequence]);

  const addNode = () => {
    session.addNode(profiles[0]?.agent_id ?? "");
    additionSequence.current += 1;
    setHighlightSequence(additionSequence.current);
  };
  const removeTransition = (editorId: string) => session.remove({ nodeIds: new Set(), endNodeIds: new Set(), transitionIds: new Set([editorId]) });
  const lastNodeIndex = [...outline.connected, ...outline.unconnected].at(-1);

  const renderNode = (index: number) => {
    const node = configuration.nodes[index];
    return <TeamNodeCard
      key={node.editorId}
      node={node}
      index={index}
      configuration={configuration}
      profiles={profiles}
      highlighted={highlightSequence !== null && index === lastNodeIndex}
      graphIssues={graphIssues}
      session={session}
      onRemoveTransition={removeTransition}
    />;
  };

  return (
    <div className="space-y-6 p-5">
      <AgentTeamConfigurationFields
        configuration={configuration}
        teamIdEditable={teamIdEditable}
        scopeEditable={scopeEditable}
        layout="form"
        onChange={session.updateConfiguration}
      />

      <section ref={nodeListRef} aria-label="Team 节点" className="space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-medium">执行流程</h3>
            <p className="text-muted-foreground text-xs">从入口节点开始，按状态转移排列</p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={addNode}><PlusIcon />添加节点</Button>
        </div>
        {configuration.nodes.length === 0 && <EmptyState className="py-6" title="添加第一个节点，它会成为 Team 的入口。" />}
        {configuration.nodes.length > 0 && <TeamGraphIssueList issues={graphIssues.teamLevel} />}
        <ol className="space-y-3">
          {outline.connected.map((index, position) => (
            <li key={configuration.nodes[index].editorId} className="relative">
              {position > 0 && <span aria-hidden="true" className="bg-border absolute -top-3 left-8 h-3 w-px" />}
              {renderNode(index)}
            </li>
          ))}
        </ol>
        {(outline.unconnected.length > 0 || outline.dangling.length > 0) && <div className="space-y-3 pt-2">
          <div className="text-muted-foreground flex items-center gap-2 text-xs">
            <UnplugIcon className="size-3.5" />
            <span className="font-medium">未连接</span>
            <span>从入口出发走不到，或来源节点不存在</span>
          </div>
          {outline.unconnected.map(renderNode)}
          {outline.dangling.length > 0 && <div className="border-warning/30 bg-warning/5 space-y-2 rounded-2xl border border-dashed p-4" aria-label="来源无效的转移">
            <p className="text-warning text-xs font-medium">以下转移的来源节点不存在或 ID 重复</p>
            {outline.dangling.map((transitionIndex) => {
              const transition = configuration.transitions[transitionIndex];
              return <div key={transition.editorId} className="space-y-1">
                <p className="text-muted-foreground text-2xs">来源：{transition.from_node_id || "（空）"}</p>
                <TeamTransitionFields
                  transition={transition}
                  source={undefined}
                  nodes={configuration.nodes}
                  onChange={(changes) => session.editTransition(transitionIndex, { ...transition, ...changes })}
                  onDelete={() => removeTransition(transition.editorId)}
                />
                <TeamGraphIssueList className="pl-4" issues={graphIssues.byTransitionIndex.get(transitionIndex) ?? []} />
              </div>;
            })}
          </div>}
        </div>}
      </section>

      {profiles.length === 0 && <EmptyState size="compact" title="当前作用域暂无子 Agent 配置，可先添加节点草稿并稍后选择 Agent。" />}
      <div className="flex justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={onCancel}>取消</Button><Button type="button" onClick={onSave} disabled={saving}><SaveIcon />{saving ? "保存中…" : "保存配置"}</Button></div>
    </div>
  );
}

function TeamNodeCard({ node, index, configuration, profiles, highlighted, graphIssues, session, onRemoveTransition }: {
  node: EditorNode;
  index: number;
  configuration: EditorDraft;
  profiles: AgentConfiguration[];
  highlighted: boolean;
  graphIssues: TeamGraphIssues;
  session: TeamEditorSession;
  onRemoveTransition: (editorId: string) => void;
}) {
  const isStart = node.node_id === configuration.start_node_id;
  const role = profiles.find((profile) => profile.agent_id === node.agent_id)?.role;
  // ID 重复时转移无法确定归属，统一放进「来源无效」分组，避免在两张卡片下重复出现。
  const uniqueId = configuration.nodes.filter((item) => item.node_id === node.node_id).length === 1;
  const outgoing = uniqueId
    ? configuration.transitions.flatMap((transition, transitionIndex) => transition.from_node_id === node.node_id ? [{ transition, transitionIndex }] : [])
    : [];
  const addTransition = () => session.editTransition(null, {
    from_node_id: node.node_id,
    status: node.statuses[0]?.name ?? "",
    target_node_id: "END",
  });
  const label = node.name || node.node_id;
  // 转移上的问题显示在对应转移行下，卡片头部只列节点本身的问题，避免重复。
  const nodeIssues = (uniqueId ? graphIssues.byNodeId.get(node.node_id) ?? [] : []).filter((issue) => issue.transition_index === null);

  return (
    <article
      data-team-entry-card
      aria-label={`节点 ${label}`}
      className={`bg-card overflow-hidden rounded-2xl border ${nodeIssues.length ? "border-warning/50" : isStart ? "border-primary/40" : ""} ${highlighted ? "agent-team-entry-highlight" : ""}`}
    >
      <header className="bg-muted/30 flex items-center gap-3 border-b px-4 py-3">
        <AgentAvatar agentId={node.agent_id} />
        <div className="min-w-0 flex-1">
          <h4 className="truncate text-sm font-semibold">{label}</h4>
          <p className={`truncate text-2xs ${node.agent_id ? "text-muted-foreground" : "text-warning"}`}>
            {node.agent_id ? `${node.agent_id}${role ? ` · ${role}` : ""}` : "尚未分配子 Agent"}
          </p>
        </div>
        {isStart && <span className="bg-primary text-primary-foreground inline-flex h-5 items-center gap-1 rounded-full px-2 text-2xs font-semibold tracking-wide"><PlayIcon aria-hidden="true" className="size-2.5 fill-current" />START</span>}
      </header>
      <div className="space-y-5 p-4">
        <TeamGraphIssueList issues={nodeIssues} />
        <TeamNodeFields
          key={node.editorId}
          node={node}
          ordinal={index + 1}
          isStart={isStart}
          profiles={profiles}
          surface="form"
          onUpdate={(changes) => session.updateNode(index, changes)}
          onSetStart={() => session.updateConfiguration({ start_node_id: node.node_id })}
          onDelete={() => session.remove({ nodeIds: new Set([node.editorId]), endNodeIds: new Set(), transitionIds: new Set() })}
        />
        <section aria-label={`${label} 的转移`} className="space-y-2 border-t pt-4">
          <div className="flex items-center justify-between">
            <h5 className="text-xs font-medium">完成后交给</h5>
            <Button type="button" variant="ghost" size="sm" aria-label={`为 ${label} 添加转移`} onClick={addTransition}><PlusIcon />添加转移</Button>
          </div>
          {outgoing.length === 0 && <p className="text-muted-foreground text-xs">尚无转移，按状态选择下一步交给哪个节点或 END。</p>}
          {outgoing.map(({ transition, transitionIndex }) => <div key={transition.editorId} className="space-y-1">
            <TeamTransitionFields
              transition={transition}
              source={node}
              nodes={configuration.nodes}
              onChange={(changes) => session.editTransition(transitionIndex, { ...transition, ...changes })}
              onDelete={() => onRemoveTransition(transition.editorId)}
            />
            <TeamGraphIssueList className="pl-4" issues={graphIssues.byTransitionIndex.get(transitionIndex) ?? []} />
          </div>)}
        </section>
      </div>
    </article>
  );
}
