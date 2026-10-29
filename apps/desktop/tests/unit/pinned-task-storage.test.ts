import { beforeEach, describe, expect, it, vi } from "vitest";

import { readPinnedTaskIds, setTaskPinned, subscribePinnedTaskIds } from "@/lib/pinned-task-storage";

const storage = new Map<string, string>();

beforeEach(() => {
  storage.clear();
  Object.assign(globalThis, { window: {
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
    },
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  } });
});

describe("pinned task storage", () => {
  it("keeps the most recently pinned task first and removes unpinned ones", () => {
    setTaskPinned(1, true);
    setTaskPinned(2, true);
    setTaskPinned(1, true);
    expect(readPinnedTaskIds()).toEqual([1, 2]);
    setTaskPinned(1, false);
    expect(readPinnedTaskIds()).toEqual([2]);
  });

  it("ignores malformed data and returns a stable snapshot", () => {
    storage.set("cosir:pinned-tasks:v1", JSON.stringify([3, "4", -1, 3, 1.5, 5]));
    const first = readPinnedTaskIds();
    expect(first).toEqual([3, 5]);
    expect(readPinnedTaskIds()).toBe(first);
    storage.set("cosir:pinned-tasks:v1", "{broken");
    expect(readPinnedTaskIds()).toEqual([]);
  });

  it("notifies subscribers in the same WebView", () => {
    const listener = vi.fn();
    const unsubscribe = subscribePinnedTaskIds(listener);
    setTaskPinned(7, true);
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});
