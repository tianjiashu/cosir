import { describe, expect, it, beforeEach } from "vitest";
import type { BackendStatusResponse } from "@shared/backend";
import { useBackendStore } from "../stores/backendStore";

const RUNNING_SNAPSHOT: BackendStatusResponse = {
  status: "running",
  managed: true,
  pid: 12345,
  port: 8000,
  startedAt: "2026-07-14T10:00:00Z",
  health: {
    status: "ok",
    modelProvider: "openai-compatible",
    modelBaseUrl: "https://api.deepseek.com",
    modelName: "deepseek-v4-flash",
    modelThinkingMode: "disabled",
    hasModelApiKey: true,
  },
  lastError: null,
  repoRoot: "/repo",
  backendDir: "/repo/apps/backend",
  pythonBinary: "/repo/apps/backend/.venv/bin/python",
};

describe("backendStore", () => {
  beforeEach(() => {
    useBackendStore.getState().reset();
  });

  it("applies a backend snapshot and updates status", () => {
    useBackendStore.getState().applySnapshot(RUNNING_SNAPSHOT);

    const state = useBackendStore.getState();
    expect(state.status).toBe("running");
    expect(state.snapshot?.pid).toBe(12345);
    expect(state.snapshot?.health?.modelName).toBe("deepseek-v4-flash");
  });

  it("records a transport error without mutating the snapshot", () => {
    useBackendStore.getState().applySnapshot(RUNNING_SNAPSHOT);
    useBackendStore.getState().setTransportError("invoke failed");

    const state = useBackendStore.getState();
    expect(state.transportError).toBe("invoke failed");
    expect(state.snapshot?.status).toBe("running");
  });
});
