import { beforeEach, describe, expect, it } from "vitest";

import {
  readStoredToolGroupSelection,
  removeStoredToolGroupSelection,
  writeStoredToolGroupSelection,
} from "@/lib/tool-group-selection-storage";

const storage = new Map<string, string>();

beforeEach(() => {
  storage.clear();
  Object.assign(globalThis, { window: { localStorage: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  } } });
});

describe("task tool-group selection storage", () => {
  it("stores an explicit empty selection separately from a missing preference", () => {
    expect(readStoredToolGroupSelection(42)).toBeNull();
    writeStoredToolGroupSelection(42, []);
    expect(readStoredToolGroupSelection(42)).toEqual([]);
  });

  it("deduplicates selected groups and isolates tasks", () => {
    writeStoredToolGroupSelection(42, ["files", "files", "search"]);
    expect(readStoredToolGroupSelection(42)).toEqual(["files", "search"]);
    expect(readStoredToolGroupSelection(43)).toBeNull();
  });

  it("rejects malformed stored data and removes a provisional task preference", () => {
    storage.set("cosir:tool-group-selection:task:42", JSON.stringify(["files", 1]));
    expect(readStoredToolGroupSelection(42)).toBeNull();
    writeStoredToolGroupSelection(42, ["files"]);
    removeStoredToolGroupSelection(42);
    expect(readStoredToolGroupSelection(42)).toBeNull();
  });
});
