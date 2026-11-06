import { describe, it, expect } from "vitest";
import { currentActivity } from "@/lib/assistant/current-activity";
import { activity, assistantMessage, transportState, toolCallPart } from "./transport-fixtures";

describe("current activity facts", () => {
  it("does not require an assistant part or reasoning body", () => {
    const state = transportState({ activity: activity(1, "running", { modelActivity: { stepId: "step-4", phase: "reasoning" } }) });
    expect(currentActivity(state)?.label).toBe("正在思考");
    expect(currentActivity(state, true)?.label).toBe("正在停止…");
    state.activity = activity(1, "cancelled");
    expect(currentActivity(state)).toBeNull();
  });
  it("quiet controls cannot erase progress and background work does not imply waiting", () => {
    const state = transportState({ activity: activity(1, "running"), messages: [assistantMessage("a", 1, [toolCallPart("wait", "wait_messages", { presentation: "quiet" })])] });
    expect(currentActivity(state)?.label).toBe("正在处理");
    state.activity = activity(1, "suspended");
    expect(currentActivity(state)?.label).toBe("等待继续");
  });
  it("does not duplicate visible tool activity, but the next model step is visible", () => {
    const state = transportState({ activity: activity(1, "running"), messages: [assistantMessage("a", 1, [toolCallPart("read", "read_file")])] });
    expect(currentActivity(state)).toBeNull();
    state.activity = activity(1, "running", { modelActivity: { stepId: "step-5", phase: "waiting" } });
    expect(currentActivity(state)?.label).toBe("正在等待模型响应");
  });
});
