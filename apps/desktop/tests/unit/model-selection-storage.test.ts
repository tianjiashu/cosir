import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  readStoredSelection,
  selectionStorageKey,
  subscribeStoredSelection,
  writeStoredSelection,
} from "@/lib/model-selection-storage";

const storage = new Map<string, string>();

beforeEach(() => {
  storage.clear();
  Object.assign(globalThis, {
    window: {
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
      },
    },
  });
});

describe("model selection storage scopes", () => {
  it("uses separate workspace and task keys without fallback leakage", () => {
    const selection = { providerId: 1, modelName: "demo", reasoningEffort: null as null };
    writeStoredSelection({ kind: "workspace", id: 9 }, selection);

    expect(selectionStorageKey({ kind: "workspace", id: 9 })).toBe("cosir:model-selection:workspace:9");
    expect(selectionStorageKey({ kind: "task", id: 9 })).toBe("cosir:model-selection:task:9");
    expect(readStoredSelection({ kind: "workspace", id: 9 })).toEqual(selection);
    expect(readStoredSelection({ kind: "task", id: 9 })).toBeNull();
  });

  it("notifies another selector in the same WebView when a scoped choice changes", () => {
    const scope = { kind: "task" as const, id: 12 };
    const listener = vi.fn();
    const unsubscribe = subscribeStoredSelection(scope, listener);

    writeStoredSelection(scope, { providerId: 2, modelName: "new-model", reasoningEffort: null });

    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it("treats unavailable localStorage as an empty preference store", () => {
    Object.assign(window, {
      localStorage: {
        getItem: () => { throw new Error("storage unavailable"); },
        setItem: () => { throw new Error("storage unavailable"); },
      },
    });

    expect(readStoredSelection({ kind: "task", id: 12 })).toBeNull();
    expect(() => writeStoredSelection({ kind: "task", id: 12 }, {
      providerId: 2,
      modelName: "model",
      reasoningEffort: null,
    })).not.toThrow();
  });
});
