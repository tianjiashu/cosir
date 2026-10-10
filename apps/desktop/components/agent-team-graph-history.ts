import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";
import type { AgentTeamGraphLayout } from "@/components/agent-team-graph-model";

export type AgentTeamGraphSnapshot = {
  configuration: AgentTeamConfiguration;
  layout: AgentTeamGraphLayout;
};

export type AgentTeamGraphEditHistory = {
  past: AgentTeamGraphSnapshot[];
  future: AgentTeamGraphSnapshot[];
  lastCoalesceKey: string | null;
  lastCommitAt: number | null;
};

type GraphEditOptions = {
  coalesceKey?: string;
  now?: number;
};

const MAX_HISTORY_ENTRIES = 50;
const COALESCE_WINDOW_MS = 500;

export function createGraphEditHistory(): AgentTeamGraphEditHistory {
  return { past: [], future: [], lastCoalesceKey: null, lastCommitAt: null };
}

/**
 * 把配置和本地画布布局作为同一编辑单元记录，避免撤销节点或连线时留下半份状态。
 * 连续输入和方向键移动可在短时间窗口内合并，新的编辑会清除重做分支。
 */
export function commitGraphEdit(
  history: AgentTeamGraphEditHistory,
  current: AgentTeamGraphSnapshot,
  next: AgentTeamGraphSnapshot,
  options: GraphEditOptions = {},
): AgentTeamGraphEditHistory {
  const now = options.now ?? Date.now();
  const coalesces = options.coalesceKey !== undefined
    && history.lastCoalesceKey === options.coalesceKey
    && history.lastCommitAt !== null
    && now - history.lastCommitAt <= COALESCE_WINDOW_MS;

  return {
    past: coalesces ? history.past : [...history.past, current].slice(-MAX_HISTORY_ENTRIES),
    future: [],
    lastCoalesceKey: options.coalesceKey ?? null,
    lastCommitAt: now,
  };
}

/** 当前文档留在编辑器状态中，历史只保存可回退的快照。 */
export function undoGraphEdit(
  history: AgentTeamGraphEditHistory,
  current: AgentTeamGraphSnapshot,
): { history: AgentTeamGraphEditHistory; snapshot: AgentTeamGraphSnapshot } | null {
  const snapshot = history.past.at(-1);
  if (!snapshot) return null;
  return {
    snapshot,
    history: {
      past: history.past.slice(0, -1),
      future: [current, ...history.future].slice(0, MAX_HISTORY_ENTRIES),
      lastCoalesceKey: null,
      lastCommitAt: null,
    },
  };
}

export function redoGraphEdit(
  history: AgentTeamGraphEditHistory,
  current: AgentTeamGraphSnapshot,
): { history: AgentTeamGraphEditHistory; snapshot: AgentTeamGraphSnapshot } | null {
  const snapshot = history.future[0];
  if (!snapshot) return null;
  return {
    snapshot,
    history: {
      past: [...history.past, current].slice(-MAX_HISTORY_ENTRIES),
      future: history.future.slice(1),
      lastCoalesceKey: null,
      lastCommitAt: null,
    },
  };
}
