"use client";

import type { ReactNode } from "react";
import { ArrowRightIcon, CheckCircle2Icon, RotateCcwIcon, Trash2Icon, UsersIcon, XIcon } from "lucide-react";

import { AgentTeamConfigurationFields } from "@/components/agent-team-configuration-fields";
import { TeamGraphIssueList } from "@/components/agent-team-graph-issues";
import { AgentAvatar } from "@/components/agent-team-graph-nodes";
import { TeamNodeFields, TeamTransitionFields } from "@/components/agent-team-node-fields";
import { Button } from "@/components/ui/button";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamNodeConfiguration, AgentTeamTransitionConfiguration } from "@/lib/api/agent-teams";
import type { EditorDraft } from "@/lib/agent-team-editor/model";
import type { TeamGraphIssues } from "@/lib/agent-team-editor/use-graph-issues";

/** 画布当前选中的对象；未选中时属性面板展示 Team 信息。 */
export type GraphInspectorSelection =
  | { kind: "agent"; nodeIndex: number }
  | { kind: "terminal"; endNodeId: string }
  | { kind: "transition"; transitionIndex: number; hasCustomRoute: boolean }
  | null;

type InspectorActions = {
  onDismiss: () => void;
  onUpdateNode: (nodeIndex: number, changes: Partial<AgentTeamNodeConfiguration>) => void;
  onSetStartNode: (nodeId: string) => void;
  onDeleteNode: (nodeIndex: number) => void;
  onDeleteEndNode: (endNodeId: string) => void;
  onUpdateTransition: (transitionIndex: number, changes: Partial<AgentTeamTransitionConfiguration>) => void;
  onDeleteTransition: (transitionId: string) => void;
  onResetRoute: (routeKey: string) => void;
  onUpdateTeam: (changes: Partial<Omit<EditorDraft, "nodes" | "transitions">>, historyKey?: string) => void;
};

/**
 * Agent Team 画布右侧的属性面板：按选中对象切换节点、结束出口、转移或 Team 信息（selection 为 null）。
 *
 * 取代原先跟随节点浮动的属性弹层与「Team 设置」对话框：编辑入口固定在画布右侧一处，
 * 不需要按节点位置计算弹出方向。面板浮在画布上而不占布局宽度，设置页内嵌的窄画布
 * 也不会被挤压；何时显示由图编辑器决定。面板只发起编辑意图，草稿、历史与保存仍归编辑会话。
 */
export function AgentTeamGraphInspector({
  selection,
  graphIssues,
  configuration,
  profiles,
  endNodeCount,
  teamIdEditable,
  scopeEditable,
  actions,
}: {
  selection: GraphInspectorSelection;
  graphIssues: TeamGraphIssues;
  configuration: EditorDraft;
  profiles: AgentConfiguration[];
  endNodeCount: number;
  teamIdEditable: boolean;
  scopeEditable: boolean;
  actions: InspectorActions;
}) {
  let content: ReactNode = null;
  if (selection?.kind === "agent") {
    const node = configuration.nodes[selection.nodeIndex];
    const profile = profiles.find((item) => item.agent_id === node.agent_id);
    content = <InspectorSection
      label="节点属性编辑器"
      icon={<AgentAvatar agentId={node.agent_id} className="size-8" />}
      title={node.name || node.node_id}
      subtitle={profile ? `${profile.agent_id} · ${profile.role}` : "尚未分配子 Agent"}
      onClose={actions.onDismiss}
      closeLabel="关闭节点属性"
    >
      <TeamGraphIssueList issues={graphIssues.byNodeId.get(node.node_id) ?? []} />
      <TeamNodeFields
        key={node.editorId}
        node={node}
        ordinal={selection.nodeIndex + 1}
        isStart={node.node_id === configuration.start_node_id}
        profiles={profiles}
        surface="canvas"
        onUpdate={(changes) => actions.onUpdateNode(selection.nodeIndex, changes)}
        onSetStart={() => actions.onSetStartNode(node.node_id)}
        onDelete={() => actions.onDeleteNode(selection.nodeIndex)}
      />
    </InspectorSection>;
  } else if (selection?.kind === "terminal") {
    content = <InspectorSection
      label="结束出口属性"
      icon={<CheckCircle2Icon className="size-4" />}
      tone="success"
      title="END"
      subtitle="Team 在这里结束"
      onClose={actions.onDismiss}
      closeLabel="关闭结束出口属性"
    >
      <p className="text-muted-foreground text-xs leading-5">多个 END 只是画布上的不同落点，保存时都指向同一个结束出口。</p>
      <Button type="button" variant="outline" size="sm" className="text-destructive w-full" disabled={endNodeCount <= 1} onClick={() => actions.onDeleteEndNode(selection.endNodeId)}>
        <Trash2Icon />{endNodeCount <= 1 ? "至少保留一个 END" : "删除这个 END"}
      </Button>
    </InspectorSection>;
  } else if (selection?.kind === "transition") {
    const transition = configuration.transitions[selection.transitionIndex];
    const source = configuration.nodes.find((node) => node.node_id === transition.from_node_id);
    content = <InspectorSection
      label="转移属性"
      icon={<ArrowRightIcon className="size-4" />}
      title="状态转移"
      subtitle={`来源：${source ? source.name || source.node_id : transition.from_node_id || "（未设置）"}`}
      onClose={actions.onDismiss}
      closeLabel="关闭转移属性"
    >
      <TeamGraphIssueList issues={graphIssues.byTransitionIndex.get(selection.transitionIndex) ?? []} />
      <TeamTransitionFields
        transition={transition}
        source={source}
        nodes={configuration.nodes}
        onChange={(changes) => actions.onUpdateTransition(selection.transitionIndex, changes)}
        onDelete={() => actions.onDeleteTransition(transition.editorId)}
      />
      <p className="text-muted-foreground text-2xs leading-4">也可以直接拖动连线末端更换目标；拖动线上的控制点可调整路径。</p>
      {selection.hasCustomRoute && <Button type="button" variant="outline" size="sm" className="w-full" onClick={() => actions.onResetRoute(transition.editorId)}><RotateCcwIcon />恢复自动路径</Button>}
    </InspectorSection>;
  } else {
    const startNode = configuration.nodes.find((node) => node.node_id === configuration.start_node_id);
    content = <InspectorSection
      label="Team 属性"
      icon={<UsersIcon className="size-4" />}
      title={configuration.name || "未命名 Team"}
      subtitle="选中节点或连线可编辑它们"
      onClose={actions.onDismiss}
      closeLabel="关闭 Team 属性"
    >
      <dl className="grid grid-cols-3 gap-2 text-center">
        <TeamStat label="节点" value={configuration.nodes.length} />
        <TeamStat label="转移" value={configuration.transitions.length} />
        <TeamStat label="入口" value={startNode ? startNode.name || startNode.node_id : "—"} />
      </dl>
      <TeamGraphIssueList issues={graphIssues.teamLevel} />
      <AgentTeamConfigurationFields
        configuration={configuration}
        teamIdEditable={teamIdEditable}
        scopeEditable={scopeEditable}
        layout="panel"
        onChange={actions.onUpdateTeam}
      />
    </InspectorSection>;
  }

  return (
    <aside
      className="nodrag nopan nowheel bg-card absolute top-3 right-3 z-10 flex max-h-[calc(100%-1.5rem)] w-72 flex-col overflow-y-auto rounded-2xl border shadow-xl"
      aria-label="属性面板"
    >
      {content}
    </aside>
  );
}

function InspectorSection({ label, icon, tone, title, subtitle, onClose, closeLabel, children }: {
  label: string;
  icon: ReactNode;
  tone?: "success";
  title: ReactNode;
  subtitle: string;
  onClose?: () => void;
  closeLabel?: string;
  children: ReactNode;
}) {
  return (
    <section aria-label={label} className="flex flex-col">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        <div className={`flex size-8 shrink-0 items-center justify-center rounded-lg ${tone === "success" ? "bg-success/10 text-success" : "bg-muted text-foreground"}`}>{icon}</div>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold">{title}</h3>
          <p className="text-muted-foreground truncate text-2xs">{subtitle}</p>
        </div>
        {onClose && <Button type="button" variant="ghost" size="icon-sm" aria-label={closeLabel} onClick={onClose}><XIcon /></Button>}
      </header>
      <div className="space-y-4 p-4">{children}</div>
    </section>
  );
}

function TeamStat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="bg-muted/40 min-w-0 rounded-lg px-2 py-2">
      <dd className="truncate text-sm font-semibold">{value}</dd>
      <dt className="text-muted-foreground text-2xs">{label}</dt>
    </div>
  );
}
