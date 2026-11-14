import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  readStoredSelection,
  selectionStorageKey,
  subscribeStoredSelection,
  writeStoredSelection,
} from "@/lib/model/selection-storage";

const storage = new Map<string, string>();

beforeEach(() => {
  storage.clear();
  Object.assign(globalThis, { window: { localStorage: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  } } });
});

describe("model selection storage scopes", () => {
  it("stores only model configuration identity and effort per scope", () => {
    const selection = { modelConfigId: "c/m1", reasoningEffort: null as null };
    writeStoredSelection({ kind: "workspace", id: 9 }, selection);
    expect(selectionStorageKey({ kind: "workspace", id: 9 })).toBe("cosir:model-config-selection:workspace:9");
    expect(selectionStorageKey({ kind: "session", id: 9 })).toBe("cosir:model-config-selection:session:9");
    expect(readStoredSelection({ kind: "workspace", id: 9 })).toEqual(selection);
    expect(readStoredSelection({ kind: "session", id: 9 })).toBeNull();
  });

  it("ignores selections stored before model ids became strings", () => {
    const scope = { kind: "workspace" as const, id: 3 };
    window.localStorage.setItem(selectionStorageKey(scope), JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
    expect(readStoredSelection(scope)).toBeNull();
  });

  it("notifies another selector in the same WebView when a choice changes", () => {
    const scope = { kind: "session" as const, id: 12 };
    const listener = vi.fn();
    const unsubscribe = subscribeStoredSelection(scope, listener);
    writeStoredSelection(scope, { modelConfigId: "c/m2", reasoningEffort: null });
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it("treats unavailable localStorage as an empty preference store", () => {
    Object.assign(window, { localStorage: {
      getItem: () => { throw new Error("storage unavailable"); },
      setItem: () => { throw new Error("storage unavailable"); },
    } });
    expect(readStoredSelection({ kind: "session", id: 12 })).toBeNull();
    expect(() => writeStoredSelection({ kind: "session", id: 12 }, { modelConfigId: "c/m2", reasoningEffort: null })).not.toThrow();
  });
});
