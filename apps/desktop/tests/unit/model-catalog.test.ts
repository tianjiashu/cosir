import { describe, expect, it, vi } from "vitest";

const getModelGroups = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/models", () => ({ getModelGroups }));

const groups = (modelName: string) => [{
  provider_id: 1,
  provider_name: "demo",
  models: [{
    model_name: modelName,
    supports_thinking: true,
    supports_image: false,
    supports_video: false,
    supports_reasoning_effort: true,
  }],
}];

describe("global model catalog", () => {
  it("aborts the previous load and ignores its late response", async () => {
    let resolveFirst: ((value: ReturnType<typeof groups>) => void) | undefined;
    let resolveSecond: ((value: ReturnType<typeof groups>) => void) | undefined;
    const first = new Promise<ReturnType<typeof groups>>((resolve) => { resolveFirst = resolve; });
    const second = new Promise<ReturnType<typeof groups>>((resolve) => { resolveSecond = resolve; });
    getModelGroups
      .mockImplementationOnce((init: RequestInit) => {
        expect(init.signal).toBeInstanceOf(AbortSignal);
        return first;
      })
      .mockImplementationOnce((init: RequestInit) => {
        expect(init.signal).toBeInstanceOf(AbortSignal);
        return second;
      });

    const { getModelCatalogSnapshot, loadModelCatalog } = await import("@/lib/model-catalog");
    const firstLoad = loadModelCatalog();
    const secondLoad = loadModelCatalog({ force: true });
    expect(getModelGroups.mock.calls[0]?.[0].signal.aborted).toBe(true);

    resolveFirst?.(groups("stale-model"));
    resolveSecond?.(groups("current-model"));
    await Promise.all([firstLoad, secondLoad]);

    expect(getModelCatalogSnapshot().catalog?.models[0]?.modelName).toBe("current-model");
    expect(getModelCatalogSnapshot().status).toBe("ready");
  });
});
