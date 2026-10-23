import { describe, expect, it } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import { activeRunLaunch } from "@/lib/assistant/run-launch";

const LAUNCH = { modelConfigId: 3, reasoningEffort: "high", banTools: ["a", "b"] };

const state = (status: string): TransportState => ({
  runs: [{
    runId: 1, status, endReason: null, contextWindowTotal: null, launch: LAUNCH,
    messages: [], usage: null, error: null,
  }],
  current_run_id: 1,
  approvals: {},
  error: null,
});

describe("run launch lock", () => {
  it.each(["running", "suspended"])("locks to the %s run's launch", (status) => {
    expect(activeRunLaunch(state(status))).toBe(LAUNCH);
  });

  it.each(["completed", "failed", "cancelled"])("releases the lock after the run is %s", (status) => {
    expect(activeRunLaunch(state(status))).toBeNull();
  });

  it("has no lock before the task has a run", () => {
    expect(activeRunLaunch(null)).toBeNull();
    expect(activeRunLaunch({ ...state("running"), current_run_id: null })).toBeNull();
  });
});
