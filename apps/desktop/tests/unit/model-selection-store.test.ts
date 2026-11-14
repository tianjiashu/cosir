import { beforeEach, describe, expect, it } from "vitest";

import type { ModelCatalogModel } from "@/lib/model/catalog";
import { normalizeModelSelections, resolveModelSelection, useModelSelectionStore } from "@/lib/model/model-selection-store";

beforeEach(() => useModelSelectionStore.setState({ sessions: {}, workspaces: {} }));

function model(modelConfigId: string, supportsReasoningEffort = true): ModelCatalogModel {
  return {
    optionId: modelConfigId,
    modelConfigId,
    connectionName: "c",
    modelName: modelConfigId,
    contextWindowK: 128,
    label: modelConfigId,
    supportsReasoningEffort,
    supportsImage: false,
  };
}

const models = [model("c/first"), model("c/session"), model("c/last"), model("c/fork"), model("c/workspace"), model("c/plain", false)];
const scope = { sessionId: 5, workspaceId: 9 };
const noFacts = { lastLaunch: null, inheritedLaunch: null };
const facts = {
  lastLaunch: { modelConfigId: "c/last", reasoningEffort: "max" },
  inheritedLaunch: { modelConfigId: "c/fork", reasoningEffort: "low" },
};

describe("model selection store", () => {
  it("records a choice for the session and as the workspace default", () => {
    useModelSelectionStore.getState().choose(scope, { modelConfigId: "c/session", reasoningEffort: "low" });
    const { sessions, workspaces } = useModelSelectionStore.getState();
    expect(sessions[5]).toEqual({ modelConfigId: "c/session", reasoningEffort: "low" });
    expect(workspaces[9]).toEqual({ modelConfigId: "c/session", reasoningEffort: "low" });
  });

  it("forgets deleted sessions and workspaces separately", () => {
    const store = useModelSelectionStore.getState();
    store.choose(scope, { modelConfigId: "c/session", reasoningEffort: null });
    store.forgetSessions([5]);
    expect(useModelSelectionStore.getState().sessions[5]).toBeUndefined();
    expect(useModelSelectionStore.getState().workspaces[9]).toBeDefined();
    store.forgetWorkspace(9);
    expect(useModelSelectionStore.getState().workspaces[9]).toBeUndefined();
  });

  it("drops stored entries that are malformed or predate string model ids", () => {
    expect(normalizeModelSelections({
      sessions: { 1: { modelConfigId: 2, reasoningEffort: null }, 2: { modelConfigId: "c/ok", reasoningEffort: "low" } },
      workspaces: "broken",
    })).toEqual({ sessions: { 2: { modelConfigId: "c/ok", reasoningEffort: "low" } }, workspaces: {} });
  });
});

describe("resolveModelSelection", () => {
  it("prefers the session choice, then the last run, then the fork, then the workspace, then the first model", () => {
    const workspaces = { 9: { modelConfigId: "c/workspace", reasoningEffort: null } };
    const sessions = { 5: { modelConfigId: "c/session", reasoningEffort: "low" as const } };
    expect(resolveModelSelection(models, { sessions, workspaces }, scope, facts)?.modelConfigId).toBe("c/session");
    expect(resolveModelSelection(models, { sessions: {}, workspaces }, scope, facts)).toEqual({ modelConfigId: "c/last", reasoningEffort: "max" });
    expect(resolveModelSelection(models, { sessions: {}, workspaces }, scope, { ...facts, lastLaunch: null })?.modelConfigId).toBe("c/fork");
    // 未开始的会话：没有 Run 也不是分叉，沿用工作区默认（缺省推理强度为 high）。
    expect(resolveModelSelection(models, { sessions: {}, workspaces }, scope, noFacts)).toEqual({ modelConfigId: "c/workspace", reasoningEffort: "high" });
    expect(resolveModelSelection(models, { sessions: {}, workspaces: {} }, scope, noFacts)?.modelConfigId).toBe("c/first");
  });

  it("skips candidates whose model was deleted and normalises effort to the model", () => {
    const selections = {
      sessions: { 5: { modelConfigId: "c/gone", reasoningEffort: "low" as const } },
      workspaces: { 9: { modelConfigId: "c/plain", reasoningEffort: "max" as const } },
    };
    expect(resolveModelSelection(models, selections, scope, noFacts)).toEqual({ modelConfigId: "c/plain", reasoningEffort: null });
    expect(resolveModelSelection([], selections, scope, noFacts)).toBeNull();
  });
});
