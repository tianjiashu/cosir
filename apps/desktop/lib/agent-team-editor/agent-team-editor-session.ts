import { createStore } from "zustand/vanilla";
import type {
  AgentTeamConfiguration, AgentTeamConfigurationDraft,
  AgentTeamNodeConfiguration, AgentTeamTransitionConfiguration,
} from "@/lib/api/agent-teams";
import { DEFAULT_AGENT_TEAM_STATUS_COLOR } from "@/lib/agent-team-status";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { showErrorDialog } from "@/lib/confirm/confirm-store";
import { createGraphEditHistory, commitGraphEdit, undoGraphEdit, redoGraphEdit, type AgentTeamGraphEditHistory } from "./history";
import {
  cleanSnapshot, createIdentityAllocator, emptyConfiguration, emptyLayout, exportDraft,
  importDraft, nextNodeId, removeSelection, updateNodeInConfiguration,
  type EditorDraft, type EditorSnapshot, type EditorStatus, type Position,
} from "./model";
import { readLayout, writeLayout, savedLayoutPreferences } from "./layout-storage";

export type PersistTeam = (
  configuration: AgentTeamConfigurationDraft,
  identity: { teamId: string; scope: AgentTeamConfiguration["scope"] } | null,
) => Promise<AgentTeamConfiguration>;

export type EditorState = {
  snapshot: EditorSnapshot;
  baseline: EditorDraft | null;
  history: AgentTeamGraphEditHistory;
  dirty: boolean;
  saving: boolean;
  mode: "form" | "canvas";
  viewport: { x: number; y: number; zoom: number } | undefined;
};

/** 数字输入框使用字符串；只在比较时统一整数表示，原始草稿仍交给后端校验。 */
function businessJSON(draft: EditorDraft): string {
  const value = exportDraft(draft);
  if (typeof value.max_runs === "string" && /^\d+$/.test(value.max_runs)) {
    const numeric = Number(value.max_runs);
    if (Number.isSafeInteger(numeric)) value.max_runs = numeric;
  }
  return JSON.stringify(value);
}

/** 服务端保持实体顺序；规范化业务字段时复用指定快照中的编辑身份。 */
function savedDraft(saved: AgentTeamConfiguration, identity: EditorDraft): EditorDraft {
  return {
    ...saved,
    nodes: saved.nodes.map((node, index) => ({
      ...node,
      editorId: identity.nodes[index].editorId,
      statuses: node.statuses.map((status, statusIndex) => ({
        ...status, editorId: identity.nodes[index].statuses[statusIndex].editorId,
      })),
    })),
    transitions: saved.transitions.map((edge, index) => ({ ...edge, editorId: identity.transitions[index].editorId })),
  };
}

/**
 * 一次编辑会话的唯一 working copy owner。配置页持有到离开，工作台标签持有到关闭。
 * 视图只提交编辑意图；保存应答更新提交快照的基线，绝不覆盖期间产生的新编辑。
 */
export function createTeamEditorSession(options: {
  initial?: AgentTeamConfigurationDraft | null;
  saved: boolean;
  scope?: AgentTeamConfiguration["scope"];
  workspaceId?: number | null;
}) {
  const allocate = createIdentityAllocator();
  const configuration = importDraft(options.initial ?? emptyConfiguration(options.scope ?? "workspace"), allocate);
  const workspaceId = options.workspaceId ?? null;
  const layout = options.saved ? readLayout(workspaceId, configuration) : emptyLayout();
  // 默认坐标也属于会话布局；否则删除前面的节点会改变后续节点的默认位置。
  configuration.nodes.forEach((node, index) => {
    layout.positions[node.editorId] ??= { x: 100, y: 80 + index * 180 };
  });
  layout.endNodeIds.forEach((id, index) => {
    layout.positions[id] ??= { x: 420, y: Math.max(80, configuration.nodes.length * 180) + index * 110 };
  });
  let persistedLayout = layout;
  const baseline = options.saved ? configuration : null;
  const store = createStore<EditorState>(() => ({
    snapshot: { configuration, layout }, baseline, history: createGraphEditHistory(),
    dirty: !options.saved && options.initial != null,
    saving: false, mode: "form", viewport: undefined,
  }));
  // 空白新建在真正修改前不询问放弃；工具生成的草稿从一开始就是未保存内容。
  const initialEmpty = options.initial == null ? businessJSON(configuration) : null;
  const dirtyAgainst = (draft: EditorDraft, saved: EditorDraft | null) => (
    businessJSON(draft) !== (saved ? businessJSON(saved) : initialEmpty)
  );
  const lockIdentity = (draft: EditorDraft): EditorDraft => {
    const saved = store.getState().baseline;
    // 已创建的配置不能经撤销回到可更换保存身份的状态。
    return saved ? { ...draft, team_id: saved.team_id, scope: saved.scope } : draft;
  };
  const publish = (snapshot: EditorSnapshot, history: AgentTeamGraphEditHistory) => {
    const state = store.getState();
    const configuration = lockIdentity(snapshot.configuration);
    store.setState({
      snapshot: { ...snapshot, configuration }, history,
      dirty: dirtyAgainst(configuration, state.baseline),
    });
    if (state.baseline) {
      persistedLayout = savedLayoutPreferences(state.baseline, snapshot, persistedLayout);
      writeLayout(workspaceId, state.baseline, persistedLayout);
    }
  };
  const commit = (next: EditorSnapshot, coalesceKey?: string) => {
    const state = store.getState();
    const snapshot = cleanSnapshot({ ...next, configuration: lockIdentity(next.configuration) });
    if (JSON.stringify(state.snapshot) === JSON.stringify(snapshot)) return;
    publish(snapshot, commitGraphEdit(state.history, state.snapshot, coalesceKey));
  };
  const updateConfiguration = (changes: Partial<Omit<EditorDraft, "nodes" | "transitions">>, coalesceKey?: string) => {
    const { snapshot } = store.getState();
    commit({ ...snapshot, configuration: { ...snapshot.configuration, ...changes } }, coalesceKey);
  };
  const updateNode = (index: number, changes: Partial<AgentTeamNodeConfiguration>) => {
    const { snapshot } = store.getState();
    const statuses = changes.statuses?.map(status => "editorId" in status
      ? status as EditorStatus : { ...status, editorId: allocate("status") });
    const nodeChanges = { ...changes, ...(statuses ? { statuses } : {}) } as Partial<EditorDraft["nodes"][number]>;
    const coalesceKey = Object.keys(changes).length === 1 && "name" in changes
      ? `node-name:${snapshot.configuration.nodes[index].editorId}` : undefined;
    commit({ ...snapshot, configuration: updateNodeInConfiguration(snapshot.configuration, index, nodeChanges) }, coalesceKey);
  };
  const addNode = (agentId: string, name?: string, position?: Position) => {
    const { snapshot } = store.getState();
    const current = snapshot.configuration;
    const node = {
      editorId: allocate("node"), node_id: nextNodeId(current.nodes),
      name: name ?? `节点 ${current.nodes.length + 1}`, agent_id: agentId,
      statuses: [{ editorId: allocate("status"), name: "done", color: DEFAULT_AGENT_TEAM_STATUS_COLOR }],
    };
    commit({
      configuration: { ...current, nodes: [...current.nodes, node], start_node_id: current.start_node_id || node.node_id },
      layout: { ...snapshot.layout, positions: {
        ...snapshot.layout.positions, [node.editorId]: position ?? { x: 100, y: 80 + current.nodes.length * 180 },
      } },
    });
    return node.editorId;
  };
  const editTransition = (index: number | null, changes: AgentTeamTransitionConfiguration, endId?: string) => {
    const { snapshot } = store.getState();
    const current = snapshot.configuration;
    const edge = { ...changes, editorId: index === null ? allocate("transition") : current.transitions[index].editorId };
    const transitions = index === null ? [...current.transitions, edge]
      : current.transitions.map((item, i) => i === index ? edge : item);
    const endTargets = { ...snapshot.layout.endTargets };
    if (endId) endTargets[edge.editorId] = endId;
    commit({ configuration: { ...current, transitions }, layout: { ...snapshot.layout, endTargets } });
    return edge.editorId;
  };
  const remove = (selection: Parameters<typeof removeSelection>[1]) => {
    const before = store.getState().snapshot;
    const next = removeSelection(before, selection);
    commit(next);
    if (next !== before) void frontendLog("INFO", "agent_team_selection_deleted", "已删除 Team 编辑选区", {
      traceId: newTraceId(), data: {
        teamId: before.configuration.team_id,
        deletedNodes: before.configuration.nodes.length - next.configuration.nodes.length,
        deletedTransitions: before.configuration.transitions.length - next.configuration.transitions.length,
      },
    });
  };
  const travel = (direction: "undo" | "redo") => {
    const state = store.getState();
    const result = (direction === "undo" ? undoGraphEdit : redoGraphEdit)(state.history, state.snapshot);
    if (!result) return;
    publish(result.snapshot, result.history);
    void frontendLog("INFO", `agent_team_graph_${direction}`, direction === "undo" ? "已撤销 Team 编辑" : "已重做 Team 编辑", {
      traceId: newTraceId(), data: { teamId: state.snapshot.configuration.team_id },
    });
  };
  return {
    store, commit, updateConfiguration, updateNode, addNode, editTransition, remove,
    allocateEnd: () => {
      let id = allocate("end");
      while (store.getState().snapshot.layout.endNodeIds.includes(id)) id = allocate("end");
      return id;
    },
    undo: () => travel("undo"), redo: () => travel("redo"),
    setMode: (mode: EditorState["mode"]) => store.setState({ mode }),
    setViewport: (viewport: EditorState["viewport"]) => store.setState({ viewport }),
    async save(persist: PersistTeam): Promise<boolean> {
      const before = store.getState();
      if (before.saving) return false;
      const submitted = before.snapshot;
      const traceId = newTraceId();
      // 保存是撤销分组的边界，提交后的输入不能与提交前的输入合并。
      store.setState({ saving: true, history: { ...before.history, lastCoalesceKey: null, lastCommitAt: null } });
      try {
        const saved = await persist(exportDraft(submitted.configuration), before.baseline
          ? { teamId: before.baseline.team_id, scope: before.baseline.scope } : null);
        const current = store.getState();
        const unchanged = businessJSON(current.snapshot.configuration) === businessJSON(submitted.configuration);
        // 用户可能删除后重建了相同业务内容；相同内容也必须保留当前编辑身份与布局。
        const baseline = savedDraft(saved, unchanged ? current.snapshot.configuration : submitted.configuration);
        const configuration = unchanged ? baseline
          : { ...current.snapshot.configuration, team_id: saved.team_id, scope: saved.scope };
        store.setState({ baseline, snapshot: { ...current.snapshot, configuration }, saving: false, dirty: dirtyAgainst(configuration, baseline) });
        persistedLayout = savedLayoutPreferences(baseline, current.snapshot, submitted.layout);
        writeLayout(workspaceId, baseline, persistedLayout);
        void frontendLog("INFO", "agent_team_configuration_saved", "Team 配置保存完成", {
          traceId, data: { teamId: saved.team_id, hasNewerChanges: !unchanged },
        });
        return true;
      } catch (error) {
        store.setState({ saving: false });
        // 草稿、历史与基线都保留，用户关掉弹窗即可接着改；失败原因只需要看一次，不进编辑器状态。
        void showErrorDialog({
          title: "保存失败",
          message: userErrorMessage(error, "保存失败，请检查配置后重试。"),
          actionLabel: "返回修改",
        });
        void frontendLog("WARNING", "agent_team_configuration_save_failed", "保存 Team 配置失败", {
          traceId, data: { teamId: submitted.configuration.team_id }, error,
        });
        return false;
      }
    },
  };
}
export type TeamEditorSession = ReturnType<typeof createTeamEditorSession>;
