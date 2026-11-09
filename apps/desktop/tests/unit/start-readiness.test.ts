import { describe, expect, it } from "vitest";

import { modelUnavailable, startBlockedReason } from "@/components/new-conversation/start-readiness";
import { buildModelCatalog } from "@/lib/model/catalog";

describe("modelUnavailable", () => {
  it("只有目录返回后确实没有模型才算缺失", () => {
    expect(modelUnavailable({ status: "loading", catalog: null })).toBe(false);
    expect(modelUnavailable({ status: "error", catalog: null })).toBe(true);
    expect(modelUnavailable({ status: "ready", catalog: buildModelCatalog([]) })).toBe(true);
    const connection = { connection_id: "demo", name: "Demo", base_url: "u", api_key: "k", models: [] };
    // 只有连接、没有模型时同样算缺失。
    expect(modelUnavailable({ status: "ready", catalog: buildModelCatalog([connection]) })).toBe(true);
    expect(modelUnavailable({
      status: "ready",
      catalog: buildModelCatalog([{
        ...connection,
        models: [{
          config_id: "demo/demo-model",
          connection_id: "demo",
          model_name: "demo-model",
          context_window_k: 128,
          supports_thinking: false,
          supports_reasoning_effort: false,
          supports_image: false,
        }],
      }]),
    })).toBe(false);
  });
});

describe("startBlockedReason", () => {
  const base = { hasWorkspaces: true, selectedWorkspaceId: 7, modelMissing: false, modelReady: true };

  it("explains missing workspace before missing model", () => {
    expect(startBlockedReason({ ...base, hasWorkspaces: false, selectedWorkspaceId: null, modelMissing: true, modelReady: false })).toBe("先添加工作区");
    expect(startBlockedReason({ ...base, selectedWorkspaceId: null })).toBe("先选择工作区");
    expect(startBlockedReason({ ...base, modelMissing: true, modelReady: false })).toBe("先配置模型");
  });

  it("stays quiet while the model catalog loads or when everything is ready", () => {
    expect(startBlockedReason({ ...base, modelReady: false })).toBeNull();
    expect(startBlockedReason(base)).toBeNull();
  });
});
