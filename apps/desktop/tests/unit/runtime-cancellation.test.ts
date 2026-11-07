import type { TransportState } from "@/lib/assistant/contract";
import { beforeEach, describe, expect, it } from "vitest";
import { beginCommand, finishCommand, forgetCommands, reconcileCommands as reconcile, useCommandRequests, callTarget, reconcileTaskStatuses, markTaskRequestsUnsynced, commandPending } from "@/lib/assistant/command-request-store";
import { activity, assistantMessage, toolCallPart, transportState, indexedState } from "./transport-fixtures";
const reconcileCommands = (taskId: number, state: TransportState) => reconcile(taskId, state, indexedState(state).toolCalls);
const current = () => useCommandRequests.getState().tasks[1];
beforeEach(() => forgetCommands([1]));
describe("cross-surface request lifetime", () => {
  it("keeps a signalled stop until canonical settlement and deduplicates across mounts", () => {
    reconcileCommands(1, transportState({ activity: activity(7, "running") }));
    const request = beginCommand(1, "task", "stop")!;
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task?.phase).toBe("syncing");
    expect(beginCommand(1, "task", "stop")).toBeNull();
    reconcileCommands(1, transportState({ activity: activity(7, "cancelled") }));
    expect(current()?.task).toBeUndefined();
  });
  it("a newer Run and a late HTTP response cannot revive the old stopping label", () => {
    reconcileCommands(1, transportState({ activity: activity(7, "running") }));
    const request = beginCommand(1, "task", "stop")!;
    reconcileCommands(1, transportState({ activity: activity(8, "running") }));
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task).toBeUndefined();
  });
  it("keeps unknown network outcomes distinct from explicit rejection", () => {
    const target = callTarget("c");
    const request = beginCommand(1, target, "decision")!;
    finishCommand(1, target, request, "unknown", "状态待同步");
    expect(current()?.[target]?.error).toBe("状态待同步");
    expect(beginCommand(1, target, "stop")).toBeNull();
    finishCommand(1, target, request, "rejected");
    expect(beginCommand(1, target, "decision")).not.toBeNull();
  });
  it("unknown idempotent outcomes permit explicit retry and ignore the old HTTP result", () => {
    const first = beginCommand(1, "task", "stop")!;
    finishCommand(1, "task", first, "unknown", "未能确认");
    expect(commandPending(current()?.task)).toBe(false);
    const retry = beginCommand(1, "task", "stop")!;
    finishCommand(1, "task", first, "accepted");
    expect(current()?.task?.token).toBe(retry.token);
  });
  it("global Task terminal facts settle child stops even after their card unmounts", () => {
    const request = beginCommand(1, "task", "stop")!;
    reconcileTaskStatuses({ 1: "completed" });
    reconcileTaskStatuses({ 1: "running" });
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task).toBeUndefined();
  });
  it("a lost Task status stream stops claiming that a possibly newer Run is stopping", () => {
    const request = beginCommand(1, "task", "stop")!;
    markTaskRequestsUnsynced();
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task?.error).toContain("待确认");
    expect(commandPending(current()?.task)).toBe(false);
  });
  it("unloaded history is not settlement; a real decision result is", () => {
    const target = callTarget("c");
    beginCommand(1, target, "decision");
    reconcileCommands(1, transportState({ messageOffset: 100 }));
    expect(current()?.[target]).toBeDefined();
    reconcileCommands(1, transportState({ messages: [assistantMessage("m", 2, [toolCallPart("c", "agent_team", { status: "completed", awaitingUser: false })])] }));
    expect(current()?.[target]).toBeUndefined();
  });
  it("a detach remains pending until canonical background or completion arrives", () => {
    const target = callTarget("c");
    const request = beginCommand(1, target, "background")!;
    finishCommand(1, target, request, "accepted");
    reconcileCommands(1, transportState({ foreground_waits: ["c"] }));
    expect(current()?.[target]).toBeDefined();
    reconcileCommands(1, transportState({ messages: [assistantMessage("m", 2, [toolCallPart("c", "execute_terminal", { status: "completed" })])] }));
    expect(current()?.[target]).toBeUndefined();
  });
});

it("HTTP completion uses already observed facts when no further SSE frame will arrive", () => {
  reconcileCommands(1, transportState({ activity: activity(7, "completed") }));
  const request = beginCommand(1, "task", "stop")!;
  finishCommand(1, "task", request, "accepted");
  expect(current()?.task).toBeUndefined();
});

it("a tool HTTP result cannot revive an SSE-settled decision", () => {
  const request = beginCommand(1, "call:c", "decision")!;
  reconcileCommands(1, transportState({ messages: [assistantMessage("m", 1, [toolCallPart("c", "agent_team", { awaitingUser: false })])] }));
  finishCommand(1, "call:c", request, "unknown", "迟到网络错误");
  expect(current()?.["call:c"]).toBeUndefined();
});

it("leaving a Task expires old completion facts without discarding an in-flight request", async () => {
  const { releaseCommandObservation } = await import("@/lib/assistant/command-request-store");
  const old = transportState({ activity: activity(7, "completed") });
  reconcileCommands(1, old);
  const outstanding = beginCommand(1, "call:unknown", "stop")!;
  releaseCommandObservation(1, old);
  expect(current()?.["call:unknown"]?.token).toBe(outstanding.token);
  reconcileTaskStatuses({ 1: "running" });
  const stop = beginCommand(1, "task", "stop")!;
  expect(stop.runId).toBeNull();
  finishCommand(1, "task", stop, "accepted");
  expect(current()?.task?.phase).toBe("syncing");
  reconcileTaskStatuses({ 1: "completed" });
  expect(current()?.task).toBeUndefined();
});

it("an old observer cleanup cannot expire a newer canonical observation", async () => {
  const { releaseCommandObservation } = await import("@/lib/assistant/command-request-store");
  const old = transportState({ activity: activity(7, "running") });
  reconcileCommands(1, old);
  reconcileCommands(1, transportState({ activity: activity(8, "running") }));
  releaseCommandObservation(1, old);
  expect(beginCommand(1, "task", "stop")?.runId).toBe(8);
});
