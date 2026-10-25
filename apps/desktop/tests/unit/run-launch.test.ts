import { describe, expect, it } from "vitest";

import { activeRunLaunch } from "@/lib/assistant/run-launch";
import { activity, transportState } from "./transport-fixtures";

const LAUNCH = { modelConfigId: 3, reasoningEffort: "high", banTools: ["a", "b"] };

const state = (status: Parameters<typeof activity>[1]) => transportState({ activity: activity(1, status, { launch: LAUNCH }) });

describe("run launch lock", () => {
  it.each(["running", "suspended"] as const)("locks to the %s activity's launch", (status) => {
    expect(activeRunLaunch(state(status))).toBe(LAUNCH);
  });

  it.each(["completed", "failed", "cancelled"] as const)("releases the lock after the activity is %s", (status) => {
    expect(activeRunLaunch(state(status))).toBeNull();
  });

  it("has no lock before the task has a run", () => {
    expect(activeRunLaunch(null)).toBeNull();
    expect(activeRunLaunch(transportState())).toBeNull();
  });
});
