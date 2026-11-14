import { beforeEach, describe, expect, it } from "vitest";

import { normalizePinnedSessions, usePinnedSessionsStore } from "@/lib/pinned-sessions-store";

beforeEach(() => usePinnedSessionsStore.setState({ ids: [] }));

describe("pinned sessions store", () => {
  it("keeps the most recently pinned session first and removes unpinned ones", () => {
    const { setPinned } = usePinnedSessionsStore.getState();
    setPinned(1, true);
    setPinned(2, true);
    setPinned(1, true);
    expect(usePinnedSessionsStore.getState().ids).toEqual([1, 2]);
    setPinned(1, false);
    expect(usePinnedSessionsStore.getState().ids).toEqual([2]);
  });

  it("forgets deleted sessions", () => {
    const { setPinned, forget } = usePinnedSessionsStore.getState();
    setPinned(3, true);
    setPinned(4, true);
    forget([3]);
    expect(usePinnedSessionsStore.getState().ids).toEqual([4]);
  });

  it("keeps only unique positive integer ids from stored data", () => {
    expect(normalizePinnedSessions({ ids: [3, "4", -1, 3, 1.5, 5] })).toEqual({ ids: [3, 5] });
    expect(normalizePinnedSessions("{broken")).toEqual({ ids: [] });
  });
});
