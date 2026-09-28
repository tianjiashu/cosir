import { describe, expect, it } from "vitest";

import { modelContextToTransportFields } from "@/lib/assistant/model-request-adapter";
import { buildModelCatalog } from "@/lib/model-catalog";

const catalog = buildModelCatalog([{
  config_id: 7,
  config_name: "Local OpenAI",
  base_url: "http://localhost:8000/v1",
  api_key: "secret",
  model_name: "reasoning-model",
  context_window_k: 32,
  api_key_configured: true,
  enabled: true,
  sort_order: 0,
  created_at: "",
  updated_at: "",
  supports_thinking: true,
  supports_image: false,
  supports_video: false,
  supports_reasoning_effort: true,
}]);

describe("ModelContext transport adapter", () => {
  it("maps the selector option id to only the model configuration identity", () => {
    const optionId = catalog.models[0]!.optionId;
    expect(modelContextToTransportFields({ modelName: optionId, reasoningEffort: "max" }, catalog)).toEqual({
      modelConfigId: 7,
      reasoningEffort: "max",
    });
  });

  it("does not turn an unknown UI option into an arbitrary backend model", () => {
    expect(modelContextToTransportFields({ modelName: "unknown-option" }, catalog)).toBeNull();
  });

  it("normalizes an effort unsupported by the localhost backend", () => {
    const optionId = catalog.models[0]!.optionId;
    expect(modelContextToTransportFields({ modelName: optionId, reasoningEffort: "medium" }, catalog)).toMatchObject({
      reasoningEffort: "high",
    });
  });
});
