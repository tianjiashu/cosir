import { describe, expect, it } from "vitest";

import { modelContextToTransportFields } from "@/lib/assistant/model-request-adapter";
import { buildModelCatalog } from "@/lib/model-catalog";

const catalog = buildModelCatalog([{
  provider_id: 7,
  provider_name: "local-openai",
  models: [{
    model_name: "reasoning-model",
    supports_thinking: true,
    supports_image: false,
    supports_video: false,
    supports_reasoning_effort: true,
  }],
}]);

describe("ModelContext transport adapter", () => {
  it("maps the official selector option id to backend model fields", () => {
    const optionId = catalog.models[0]!.optionId;
    expect(modelContextToTransportFields({ modelName: optionId, reasoningEffort: "max" }, catalog)).toEqual({
      providerId: 7,
      modelName: "reasoning-model",
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
