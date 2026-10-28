import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  backend: { backendBaseUrl: "http://127.0.0.1:4001", generation: 1, available: true },
  requestAssistantSnapshot: vi.fn(),
}));

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
vi.mock("@/src/runtime-config", () => ({ getBackendRuntimeSnapshot: () => mocks.backend }));
vi.mock("@/lib/assistant/assistant-snapshot-client", () => ({
  requestAssistantSnapshot: mocks.requestAssistantSnapshot,
}));

import type { TransportState } from "@/lib/assistant/contract";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";

const snapshot: TransportState = { title: "测试对话", messages: [], activity: null, terminal_sessions: [], deferred_results: [] };

/** 模拟一个只在 signal 中止时以其 reason 拒绝的挂起请求。 */
function hangUntilAborted() {
  mocks.requestAssistantSnapshot.mockImplementation((_taskId: number, init: { signal: AbortSignal }) => (
    new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true });
    })
  ));
}

describe("fetchCanonicalSnapshot", () => {
  beforeEach(() => {
    mocks.backend = { backendBaseUrl: "http://127.0.0.1:4001", generation: 1, available: true };
    mocks.requestAssistantSnapshot.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("returns a fresh snapshot from the same backend instance", async () => {
    mocks.requestAssistantSnapshot.mockResolvedValue(snapshot);
    await expect(fetchCanonicalSnapshot(7, { reason: "test", timeoutMs: 1_000 }))
      .resolves.toEqual({ kind: "fresh", snapshot });
  });

  it("marks the result superseded when the backend changes while the request is in flight", async () => {
    mocks.requestAssistantSnapshot.mockImplementation(async () => {
      mocks.backend = { backendBaseUrl: "http://127.0.0.1:4002", generation: 2, available: true };
      return snapshot;
    });
    await expect(fetchCanonicalSnapshot(7, { reason: "test", timeoutMs: 1_000 }))
      .resolves.toEqual({ kind: "superseded" });
  });

  it("does not issue a request while the backend is unavailable", async () => {
    mocks.backend = { backendBaseUrl: "http://127.0.0.1:0", generation: 1, available: false };
    await expect(fetchCanonicalSnapshot(7, { reason: "test", timeoutMs: 1_000 }))
      .resolves.toEqual({ kind: "superseded" });
    expect(mocks.requestAssistantSnapshot).not.toHaveBeenCalled();
  });

  it("treats an owner abort as superseded rather than a failure", async () => {
    hangUntilAborted();
    const owner = new AbortController();
    const pending = fetchCanonicalSnapshot(7, { reason: "test", timeoutMs: 1_000, signal: owner.signal });
    owner.abort();
    await expect(pending).resolves.toEqual({ kind: "superseded" });
  });

  it("throws a TimeoutError so recovery treats a hung request as a failure", async () => {
    vi.useFakeTimers();
    hangUntilAborted();
    const pending = fetchCanonicalSnapshot(7, { reason: "test", timeoutMs: 1_000 });
    const assertion = expect(pending).rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(1_000);
    await assertion;
  });

  it("propagates real request failures", async () => {
    mocks.requestAssistantSnapshot.mockRejectedValue(new Error("boom"));
    await expect(fetchCanonicalSnapshot(7, { reason: "test", timeoutMs: 1_000 })).rejects.toThrow("boom");
  });
});
