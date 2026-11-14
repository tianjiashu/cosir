/**
 * 模型选择偏好与它的回落规则。
 *
 * 这是「模型选择」的唯一规则来源：存储结构、校验与回落链都在这里，选择器与发送链路只经本模块读写。
 * 偏好分两层：会话里用户显式选过的，和工作区最近一次的选择（新会话沿用它）。用户在任何会话里换模型
 * 都同时更新这两层，所以没有「把工作区的选择复制给会话」这种交接。
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { TransportRunLaunch } from "@/lib/assistant/contract";
import type { ModelCatalogModel } from "@/lib/model/catalog";
import { REASONING_EFFORT_VALUES } from "@/lib/model/reasoning-effort";
import { preferenceStorage } from "@/lib/storage/preference-storage";

export type ModelSelection = {
  modelConfigId: string;
  reasoningEffort: (typeof REASONING_EFFORT_VALUES)[number] | null;
};

/** 一个会话的选择范围：会话自己的偏好，以及它所在工作区的默认。 */
export type ModelSelectionScope = { sessionId: number; workspaceId: number };

/**
 * 回落链里取自会话快照的两项：最近一个 Run 冻结的发起配置，以及分叉继承的发起配置。
 * 二者都是后端事实，只读不写回偏好。
 */
export type SessionLaunchFacts = {
  lastLaunch: TransportRunLaunch | null;
  inheritedLaunch: TransportRunLaunch | null;
};

type ModelSelections = {
  sessions: Readonly<Record<number, ModelSelection>>;
  workspaces: Readonly<Record<number, ModelSelection>>;
};

type ModelSelectionState = ModelSelections & {
  /** 用户在会话里换了模型：记为这个会话的选择，同时成为工作区默认。 */
  choose: (scope: ModelSelectionScope, selection: ModelSelection) => void;
  /** Session 删除后丢弃它们的选择；工作区默认不随会话删除。 */
  forgetSessions: (sessionIds: Iterable<number>) => void;
  /** 工作区删除后丢弃它的默认（它的会话由删除动作另行列出）。 */
  forgetWorkspace: (workspaceId: number) => void;
};

function isReasoningEffort(value: unknown): value is ModelSelection["reasoningEffort"] {
  return value === null || REASONING_EFFORT_VALUES.includes(value as ModelSelection["reasoningEffort"] & string);
}

function parseSelection(value: unknown): ModelSelection | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.modelConfigId !== "string" || !record.modelConfigId || !isReasoningEffort(record.reasoningEffort)) return null;
  return { modelConfigId: record.modelConfigId, reasoningEffort: record.reasoningEffort };
}

function parseSelections(raw: unknown): Record<number, ModelSelection> {
  if (typeof raw !== "object" || raw === null) return {};
  const entries = Object.entries(raw as Record<string, unknown>).flatMap(([id, value]) => {
    const selection = parseSelection(value);
    return Number.isInteger(Number(id)) && selection ? [[Number(id), selection] as const] : [];
  });
  return Object.fromEntries(entries);
}

/** 读入时收敛：不合法的条目丢弃，其余保留。 */
export function normalizeModelSelections(raw: unknown): ModelSelections {
  const value = typeof raw === "object" && raw !== null ? raw as Record<string, unknown> : {};
  return { sessions: parseSelections(value.sessions), workspaces: parseSelections(value.workspaces) };
}

export const useModelSelectionStore = create<ModelSelectionState>()(persist(
  (set) => ({
    sessions: {},
    workspaces: {},
    choose: ({ sessionId, workspaceId }, selection) => set((state) => ({
      sessions: { ...state.sessions, [sessionId]: selection },
      workspaces: { ...state.workspaces, [workspaceId]: selection },
    })),
    forgetSessions: (sessionIds) => set((state) => {
      const sessions = { ...state.sessions };
      for (const sessionId of sessionIds) delete sessions[sessionId];
      return { sessions };
    }),
    forgetWorkspace: (workspaceId) => set((state) => {
      const workspaces = { ...state.workspaces };
      delete workspaces[workspaceId];
      return { workspaces };
    }),
  }),
  {
    name: "cosir:model-selection",
    storage: preferenceStorage<ModelSelections>(),
    partialize: ({ sessions, workspaces }) => ({ sessions, workspaces }),
    merge: (persisted, current) => ({ ...current, ...normalizeModelSelections(persisted) }),
  },
));

function launchSelection(launch: TransportRunLaunch | null): ModelSelection | null {
  if (!launch?.modelConfigId) return null;
  return {
    modelConfigId: launch.modelConfigId,
    reasoningEffort: isReasoningEffort(launch.reasoningEffort) ? launch.reasoningEffort : null,
  };
}

/**
 * 会话此刻用哪个模型：会话里显式选过的 → 最近一个 Run 用的 → 分叉继承的 → 工作区默认 → 目录里第一个。
 *
 * 每一项都须仍在模型目录里才算数（配置可能已被删除）；推理强度按模型能力收敛。选择器显示与发送
 * 都按这一条链，所以不需要把默认值写回偏好。目录为空时返回 null。
 */
export function resolveModelSelection(
  models: readonly ModelCatalogModel[],
  selections: ModelSelections,
  scope: ModelSelectionScope,
  facts: SessionLaunchFacts,
): ModelSelection | null {
  const candidates = [
    selections.sessions[scope.sessionId] ?? null,
    launchSelection(facts.lastLaunch),
    launchSelection(facts.inheritedLaunch),
    selections.workspaces[scope.workspaceId] ?? null,
  ];
  for (const candidate of candidates) {
    const model = candidate && models.find((item) => item.modelConfigId === candidate.modelConfigId);
    if (model) return selectionFor(model, candidate.reasoningEffort);
  }
  const first = models[0];
  return first ? selectionFor(first, null) : null;
}

/** 模型换了或支持情况变了时的推理强度：支持则沿用（缺省 high），不支持则为 null。 */
export function selectionFor(model: ModelCatalogModel, reasoningEffort: ModelSelection["reasoningEffort"]): ModelSelection {
  return {
    modelConfigId: model.modelConfigId,
    reasoningEffort: model.supportsReasoningEffort ? reasoningEffort ?? "high" : null,
  };
}
