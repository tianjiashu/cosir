import { describe, expect, it, vi } from "vitest";

const getModelConfigs = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/model-configs", () => ({ getModelConfigs }));

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
  supports_video: false,
  supports_reasoning_effort: true,
}];

describe("global model catalog", () => {
  it("builds a flat label from configuration name, model name and context window", async () => {
    const { buildModelCatalog } = await import("@/lib/model-catalog");
    const model = buildModelCatalog(configs("demo-model")).models[0];
    expect(model?.configName).toBe("Demo 配置");
    expect(model?.label).toBe("Demo 配置 · demo-model");
    expect(model?.contextWindowK).toBe(128);
  });

  it("aborts the previous load and ignores its late response", async () => {
    let resolveFirst: ((value: ReturnType<typeof configs>) => void) | undefined;
    let resolveSecond: ((value: ReturnType<typeof configs>) => void) | undefined;
    const first = new Promise<ReturnType<typeof configs>>((resolve) => { resolveFirst = resolve; });
    const second = new Promise<ReturnType<typeof configs>>((resolve) => { resolveSecond = resolve; });
    getModelConfigs
      .mockImplementationOnce((init: RequestInit) => { expect(init.signal).toBeInstanceOf(AbortSignal); return first; })
      .mockImplementationOnce((init: RequestInit) => { expect(init.signal).toBeInstanceOf(AbortSignal); return second; });

    const { getModelCatalogSnapshot, loadModelCatalog } = await import("@/lib/model-catalog");
    const firstLoad = loadModelCatalog();
    const secondLoad = loadModelCatalog({ force: true });
    expect(getModelConfigs.mock.calls[0]?.[0].signal.aborted).toBe(true);
    resolveFirst?.(configs("stale-model"));
    resolveSecond?.(configs("current-model"));
    await Promise.all([firstLoad, secondLoad]);
    expect(getModelCatalogSnapshot().catalog?.models[0]?.modelName).toBe("current-model");
  });
});
