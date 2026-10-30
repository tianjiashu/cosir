import { describe, expect, it } from "vitest";

import { buildModelCatalog } from "@/lib/model-catalog";

const configs = (modelName: string) => [{
  config_id: 1,
  config_name: "Demo 配置",
  base_url: "https://example.com/v1",
  api_key: "secret",
  model_name: modelName,
  context_window_k: 128,
  api_key_configured: true,
  enabled: true,
  sort_order: 0,
  created_at: "",
  updated_at: "",
  supports_thinking: true,
  supports_image: false,
  supports_reasoning_effort: true,
}];

// 目录的加载、去重与失效由 react-query 负责（与模型配置管理共用 query key），这里只测纯映射。
describe("model catalog mapping", () => {
  it("builds a flat label from configuration name, model name and context window", () => {
    const model = buildModelCatalog(configs("demo-model")).models[0];
    expect(model?.configName).toBe("Demo 配置");
    expect(model?.label).toBe("Demo 配置 · demo-model");
    expect(model?.contextWindowK).toBe(128);
  });
});
