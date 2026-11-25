import { describe, expect, it } from "vitest";

import { COMPOSER_LAUNCH_BLOCKER_TEXT, resolveComposerLaunch, sessionLaunchFacts } from "@/lib/assistant/composer/composer-launch";
import type { TransportState } from "@/lib/assistant/contract";
import { buildModelCatalog, type ModelCatalogSnapshot } from "@/lib/model/catalog";

const ready: ModelCatalogSnapshot = {
  status: "ready",
  catalog: buildModelCatalog([{ id: "c", name: "本机", base_url: "", models: [
    { config_id: "c/first", model_name: "first", supports_reasoning_effort: true, supports_image: false },
    { config_id: "c/second", model_name: "second", supports_reasoning_effort: false, supports_image: false },
  ] }] as never),
};
const scope = { sessionId: 5, workspaceId: 9 };
const noFacts = { lastLaunch: null, inheritedLaunch: null };
const noPrefs = { sessions: {}, workspaces: {} };
const resolve = (overrides: Partial<Parameters<typeof resolveComposerLaunch>[0]>) =>
  resolveComposerLaunch({ catalog: ready, selections: noPrefs, scope, runLaunch: null, facts: noFacts, ...overrides });

describe("resolveComposerLaunch", () => {
  it("目录加载中、加载失败或为空时不能发，各有原因：输入框禁用发送，提交兜底用同一句话", () => {
    expect(resolve({ catalog: { status: "loading", catalog: null } })).toEqual({ selection: null, locked: false, blocker: "catalog_loading" });
    expect(resolve({ catalog: { status: "error", catalog: null } })).toMatchObject({ selection: null, blocker: "catalog_error" });
    expect(resolve({ catalog: { status: "ready", catalog: buildModelCatalog([]) } })).toMatchObject({ selection: null, blocker: "no_model" });
    expect(COMPOSER_LAUNCH_BLOCKER_TEXT.catalog_loading).toBe("模型还在加载，稍后再发送");
  });

  it("目录就绪时按回落链选：最近 Run 用过的优先于目录第一个", () => {
    expect(resolve({})).toEqual({ selection: { modelConfigId: "c/first", reasoningEffort: "high" }, locked: false, blocker: null });
    expect(resolve({ facts: { lastLaunch: { modelConfigId: "c/second", reasoningEffort: "low" }, inheritedLaunch: null } }).selection)
      .toEqual({ modelConfigId: "c/second", reasoningEffort: null });
  });

  it("运行期锁定用冻结的配置，不依赖目录；冻结配置里没有模型时不能发", () => {
    const locked = { modelConfigId: "c/deleted", reasoningEffort: "max" };
    expect(resolve({ catalog: { status: "loading", catalog: null }, runLaunch: locked }))
      .toEqual({ selection: { modelConfigId: "c/deleted", reasoningEffort: "max" }, locked: true, blocker: null });
    expect(resolve({ runLaunch: { modelConfigId: null, reasoningEffort: null } }))
      .toEqual({ selection: null, locked: true, blocker: "locked_model_deleted" });
  });

  it("回落事实取自会话快照的最近 Run 与分叉继承", () => {
    const state = { activity: { launch: { modelConfigId: "c/second", reasoningEffort: null } }, fork: { initialLaunch: { modelConfigId: "c/first", reasoningEffort: "low" } } } as unknown as TransportState;
    expect(sessionLaunchFacts(state)).toEqual({
      lastLaunch: { modelConfigId: "c/second", reasoningEffort: null },
      inheritedLaunch: { modelConfigId: "c/first", reasoningEffort: "low" },
    });
    expect(sessionLaunchFacts(null)).toEqual(noFacts);
  });
});
