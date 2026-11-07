import { describe, it, expect } from "vitest";
import { currentActivity } from "@/lib/assistant/current-activity";
import { projectCallStatus } from "@/lib/assistant/call-status";
import { projectTurn } from "@/lib/assistant/turn-layout";
import { activity, toolCallPart } from "./transport-fixtures";

const job = { toolCallId: "bg", kind: "terminal_command" as const, label: "检查导出", status: "running" as const, startedAt: "2026-01-01", endedAt: null };
describe("current activity facts", () => {
  it("shows model phases without an assistant, and combines parallel background work", () => {
    const model = activity(1, "running", { modelActivity: { stepId: "s", phase: "reasoning" } });
    expect(currentActivity(model)).toEqual({ label: "正在思考", animated: true });
    expect(currentActivity(model, [], [job])?.label).toBe("正在思考 · 后台 1 项");
    expect(currentActivity(activity(1, "cancelled"))).toBeNull();
  });
  it("uses original call intent with stable parallel count and explicit user waits", () => {
    expect(currentActivity(activity(1, "running"), [toolCallPart("early", "read_file", { args: undefined, status: "pending" })])?.label).toBe("读取文件");
    const call = toolCallPart("read", "read_file", { intent: "检查登录配置", status: "running" });
    expect(currentActivity(activity(1, "running"), [call, { ...call, toolCallId: "other" }])?.label).toBe("检查登录配置 · 另 1 项");
    expect(currentActivity(activity(1, "running"), [{ ...call, awaitingUser: true }])).toEqual({ label: "等待你的操作", animated: false });
    expect(currentActivity(activity(1, "suspended"), [], [{ ...job, status: "settling" }])).toEqual({ label: "等待并入后台结果：检查导出", animated: false });
  });
  it("distinguishes pending queue, background execution, settling, waits and terminal states", () => {
    expect(projectCallStatus("pending").label).toBe("准备执行");
    expect(projectCallStatus("pending", false, true).label).toBe("后台运行中");
    expect(projectCallStatus("pending", false, true, "settling")).toEqual({ label: "后台结果收尾中", animated: false });
    expect(projectCallStatus("pending", true, true).animated).toBe(false);
    for (const status of ["failed", "cancelled", "completed"] as const) expect(projectCallStatus(status, false, true).animated).toBe(false);
  });
  it("keeps one stable interval from thinking through tool execution and seals history at new prose", () => {
    const model = activity(1, "running", { modelActivity: { stepId: "s", phase: "reasoning" } });
    const initial = projectTurn([], "running", null, null, model);
    const call = toolCallPart("read", "read_file", { intent: "检查配置", status: "running" });
    const executing = projectTurn([call], "running", null, initial, { ...model, modelActivity: null });
    expect(executing.current?.groupKey).toBe(initial.current?.groupKey);
    expect(executing.current?.feedback.label).toBe("检查配置");
    const thinking = projectTurn([{ ...call, status: "completed" }], "running", null, executing, model);
    expect(thinking.current?.groupKey).toBe(executing.current?.groupKey);
    const writing = projectTurn([...thinking.parts, { type: "text", text: "发现原因" }], "running", null, thinking, model);
    expect(writing.current?.groupKey).not.toBe(thinking.current?.groupKey);
    expect(writing.blocks[0]).toBe(thinking.blocks[0]);
    const streamed = projectTurn([writing.parts[0], { type: "text", text: "发现原因，继续" }], "running", null, writing, model);
    expect(streamed.blocks[0]).toBe(writing.blocks[0]);
    expect(streamed.current?.groupKey).toBe(writing.current?.groupKey);
  });
});
