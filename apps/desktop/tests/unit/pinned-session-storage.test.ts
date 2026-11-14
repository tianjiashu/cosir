import { beforeEach, describe, expect, it, vi } from "vitest";

import { readPinnedSessionIds, setSessionPinned, subscribePinnedSessionIds } from "@/lib/pinned-session-storage";

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

describe("pinned session storage", () => {
  it("keeps the most recently pinned session first and removes unpinned ones", () => {
    setSessionPinned(1, true);
    setSessionPinned(2, true);
    setSessionPinned(1, true);
    expect(readPinnedSessionIds()).toEqual([1, 2]);
    setSessionPinned(1, false);
    expect(readPinnedSessionIds()).toEqual([2]);
  });

  it("ignores malformed data and returns a stable snapshot", () => {
    storage.set("cosir:pinned-sessions:v1", JSON.stringify([3, "4", -1, 3, 1.5, 5]));
    const first = readPinnedSessionIds();
    expect(first).toEqual([3, 5]);
    expect(readPinnedSessionIds()).toBe(first);
    storage.set("cosir:pinned-sessions:v1", "{broken");
    expect(readPinnedSessionIds()).toEqual([]);
  });

  it("notifies subscribers in the same WebView", () => {
    const listener = vi.fn();
    const unsubscribe = subscribePinnedSessionIds(listener);
    setSessionPinned(7, true);
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});
