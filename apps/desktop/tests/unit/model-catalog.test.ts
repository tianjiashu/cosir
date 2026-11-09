import { describe, expect, it } from "vitest";

import type { ModelConnection } from "@/lib/api/model-connections";
import { buildModelCatalog } from "@/lib/model/catalog";

const capabilities = { context_window_k: 128, supports_thinking: true, supports_reasoning_effort: true, supports_image: false };

const connection = (connectionId: string, name: string, models: string[]): ModelConnection => ({
  connection_id: connectionId,
  name,
  base_url: "https://example.com/v1",
  api_key: "secret",
  models: models.map((modelName) => ({
    config_id: `${connectionId}/${modelName}`,
    connection_id: connectionId,
    model_name: modelName,
    ...capabilities,
  })),
});

// 目录的加载、去重与失效由 react-query 负责（与模型配置管理共用 query key），这里只测纯映射。
describe("model catalog mapping", () => {
  it("flattens connections in order and keeps the connection name for grouping", () => {
    const { models, byOptionId } = buildModelCatalog([
      connection("packy", "Packy", ["qwen-a", "qwen-b"]),
      connection("deepseek", "DeepSeek", []),
      connection("local", "Local", ["org/llama"]),
    ]);

    expect(models.map((model) => [model.connectionName, model.modelName])).toEqual([
      ["Packy", "qwen-a"],
      ["Packy", "qwen-b"],
      ["Local", "org/llama"],
    ]);
    expect(models[0]?.label).toBe("Packy · qwen-a");
    expect(byOptionId.get("model-config:local/org/llama")?.contextWindowK).toBe(128);
  });
});
