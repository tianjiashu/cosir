import { describe, expect, it } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import { launchMatchesRun, resolveSendDelivery } from "@/lib/assistant/run-launch";

const LAUNCH = { modelConfigId: 3, reasoningEffort: "high", banTools: ["a", "b"] };
const REQUEST = { modelConfigId: 3, reasoningEffort: "high", banTools: ["b", "a"] };

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
  it("compares banTools as a set", () => {
    expect(launchMatchesRun(LAUNCH, REQUEST)).toBe(true);
    expect(launchMatchesRun(LAUNCH, { ...REQUEST, banTools: ["a"] })).toBe(false);
    expect(launchMatchesRun(LAUNCH, { ...REQUEST, modelConfigId: 4 })).toBe(false);
    expect(launchMatchesRun(LAUNCH, { ...REQUEST, reasoningEffort: null })).toBe(false);
  });

  it("keeps the preferred delivery when the active run has the same launch", () => {
    expect(resolveSendDelivery(state("running"), REQUEST, "immediate")).toBe("immediate");
  });

  it("forces next_run when the active run has a different launch", () => {
    expect(resolveSendDelivery(state("running"), { ...REQUEST, modelConfigId: 9 }, "immediate")).toBe("next_run");
    expect(resolveSendDelivery(state("waiting_for_input"), { ...REQUEST, modelConfigId: 9 }, "immediate")).toBe("next_run");
  });

  it("ignores the lock when no run is active", () => {
    expect(resolveSendDelivery(state("completed"), { ...REQUEST, modelConfigId: 9 }, "immediate")).toBe("immediate");
  });
});
