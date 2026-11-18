import { describe, it, expect } from "vitest";
import { segmentActivity, unownedActivity } from "@/lib/assistant/turn/current-activity";
import { projectCallStatus } from "@/lib/assistant/tools/call-status";
import { projectTurn, visibleTurnBlock, type ActivityGroup, type TurnLayout } from "@/lib/assistant/turn/turn-layout";
import type { TransportMessagePart } from "@/lib/assistant/contract";
import { activity, toolCallPart } from "./transport-fixtures";

const T0 = "2026-11-18T04:00:00.000Z";
const job = { toolCallId: "bg", kind: "terminal_command" as const, label: "检查导出", status: "running" as const, startedAt: "2026-01-01", endedAt: null };
const none = new Set<string>();
const segments = (layout: TurnLayout) => layout.blocks.filter((block): block is ActivityGroup => block.kind === "activity");
const text = (value: string): TransportMessagePart => ({ type: "text", text: value });
/** 正在流出的正文（后端在首个增量到达时建 part，状态为 running）。 */
const streaming = (value: string): TransportMessagePart => ({ type: "text", text: value, status: "running" });

describe("unowned activity", () => {
  it("names model phases and waits, with background work as a suffix only while running", () => {
    const model = activity(1, "running", { modelActivity: { stepId: "s", phase: "reasoning", startedAt: T0 } });
    expect(unownedActivity(model)).toEqual({ label: "思考中", animated: true, since: T0 });
    expect(unownedActivity(model, [job])?.label).toBe("思考中 · 后台 1 项");
    expect(unownedActivity(activity(1, "running", { modelActivity: { stepId: "s", phase: "tool_call", startedAt: T0 } }))).toMatchObject({ label: "正在准备工具" });
    expect(unownedActivity(activity(1, "running", { modelActivity: { stepId: "s", phase: "responding", startedAt: T0 } }))).toBeNull();
    // 步与步之间也是思考，不另起「正在处理」；这几十毫秒没有模型步起点，不计时。
    expect(unownedActivity(activity(1, "running"))).toEqual({ label: "思考中", animated: true, since: undefined });
    expect(unownedActivity(activity(1, "cancelled"))).toBeNull();
    expect(unownedActivity(activity(1, "suspended"))).toEqual({ label: "等待继续", animated: false });
    expect(unownedActivity(activity(1, "suspended"), [job])).toEqual({ label: "后台执行：检查导出", animated: false });
    expect(unownedActivity(activity(1, "suspended"), [{ ...job, status: "settling" }])).toEqual({ label: "等待并入后台结果：检查导出", animated: false });
  });
});

describe("segment activity", () => {
  const thinking = { label: "思考中", animated: true };
  it("an executing call takes precedence; otherwise the segment says the unowned state", () => {
    const call = toolCallPart("read", "read_file", { intent: "检查登录配置", status: "running" });
    expect(segmentActivity([call, { ...call, toolCallId: "other" }], none, thinking)?.label).toBe("检查登录配置 · 另 1 项");
    // 先串行后并行：排在前面的 pending 不是正在执行的那个。
    const queued = toolCallPart("first", "read_file", { intent: "读取说明", status: "pending" });
    expect(segmentActivity([queued, { ...call, intent: "修改配置" }], none, null)?.label).toBe("修改配置 · 另 1 项");
    expect(segmentActivity([{ ...call, status: "completed" }], none, thinking)).toBe(thinking);
    expect(segmentActivity([{ ...call, status: "completed" }], none, null)).toBeNull();
  });
  it("leaves background and awaiting calls to their own surfaces", () => {
    const detaching = toolCallPart("bg", "execute_terminal", { intent: "检查导出", status: "running" });
    // 转后台的那一瞬：看板已有条目，part 上的 background 还没写入，不能算作前台。
    expect(segmentActivity([detaching], new Set(["bg"]), null)).toBeNull();
    expect(segmentActivity([{ ...detaching, status: "pending", awaitingUser: true }], none, null)).toBeNull();
  });
});

describe("one segment, one row, one clock", () => {
  const model = activity(1, "running", { modelActivity: { stepId: "s", phase: "reasoning", startedAt: T0 } });
  const idle = { ...model, modelActivity: null };
  const responding = { ...model, modelActivity: { stepId: "s", phase: "responding" as const, startedAt: T0 } };
  const call = toolCallPart("read", "read_file", { intent: "检查配置", status: "running" });

  it("thinking, preparing, executing and thinking again are one segment with one identity", () => {
    const start = projectTurn([text("先看看。")], "running", null, null, model);
    const [thinking] = segments(start);
    expect(thinking).toMatchObject({ key: "interval:1", calls: [], live: { label: "思考中" } });
    // 模型吐一个换行、再开始调用：仍是同一段。
    const executing = projectTurn([text("先看看。"), text("\n"), call], "running", null, start, idle);
    expect(segments(executing).map(s => [s.key, s.live?.label])).toEqual([["interval:1", "检查配置"]]);
    const again = projectTurn([text("先看看。"), text("\n"), { ...call, status: "completed" }], "running", null, executing, model);
    expect(segments(again).map(s => [s.key, s.live?.label])).toEqual([["interval:1", "思考中"]]);
    // 新旁白：这段结束换回汇总，没有运行中的段（旁白本身就是反馈）。
    const prose = projectTurn([...again.parts, streaming("找到了。")], "running", null, again, responding);
    expect(segments(prose).map(s => [s.key, s.live])).toEqual([["interval:1", null]]);
  });

  it("a thinking-only segment is replaced by the prose that follows", () => {
    const thinking = projectTurn([text("先想想。")], "running", null, null, model);
    expect(segments(thinking)).toHaveLength(1);
    const prose = projectTurn([text("先想想。"), streaming("想好了。")], "running", null, thinking, responding);
    expect(segments(prose)).toEqual([]);
    // 模型已进入输出阶段、首段正文还在路上：这一段继续说思考，不先消失。
    expect(segments(projectTurn([text("先想想。")], "running", null, thinking, responding)).map(s => s.live?.label)).toEqual(["思考中"]);
  });

  it("a running standalone tool and a call awaiting the user speak for themselves", () => {
    const delegate = toolCallPart("d", "delegate_task", { status: "running", presentation: "standalone" });
    expect(segments(projectTurn([delegate], "running", null, null, idle))).toEqual([]);
    const asking = toolCallPart("q", "ask_user", { status: "pending", awaitingUser: true, presentation: "standalone" });
    expect(segments(projectTurn([asking], "suspended", null, null, activity(1, "suspended")))).toEqual([]);
    // 独立工具结束后又在思考：在它之后另起一段。
    expect(segments(projectTurn([{ ...delegate, status: "completed" }], "running", null, null, model)).map(s => s.key)).toEqual(["interval:1"]);
  });

  it("collapsing the process while running keeps what is being done visible; settled turns are quiet", () => {
    const layout = projectTurn([call, toolCallPart("d", "delegate_task", { status: "running", presentation: "standalone" })], "running", null, null, idle);
    expect(layout.blocks.map(block => visibleTurnBlock(block, false))).toEqual([true, true]);
    // 历史或继承里未结束的调用不是「正在做」。
    expect(segments(projectTurn([call], "settled"))[0].live).toBeNull();
  });

  // 目的：计时只认后端起点，换状态从头算；潜在缺陷：调用在 pending 时就开始计时，把写参数的时间算进执行。
  it("the row times the current state from backend facts only", () => {
    const callStart = "2026-11-18T04:00:20.000Z";
    const pending = { ...call, status: "pending" as const, startedAt: null };
    const writing = projectTurn([text("先看看。"), pending], "running", null, null, idle);
    expect(segments(writing)[0].live).toEqual({ label: "检查配置", animated: true, since: undefined });
    const running = projectTurn([text("先看看。"), { ...call, startedAt: callStart }], "running", null, writing, idle);
    expect(segments(running)[0].live?.since).toBe(callStart);
    // 调用之后的下一次思考取新模型步的起点，不接调用、也不接上一次思考。
    const next = "2026-11-18T04:00:21.000Z";
    const step = (id: string, since: string) => activity(1, "running", { modelActivity: { stepId: id, phase: "reasoning", startedAt: since } });
    const thinking = projectTurn([text("先看看。"), { ...call, status: "completed" }], "running", null, running, step("s2", next));
    expect(segments(thinking)[0].live).toEqual({ label: "思考中", animated: true, since: next });
    // 同一文案、不同起点：段要换新对象，否则活动行沿用旧计时。
    const later = projectTurn(thinking.parts, "running", null, thinking, step("s3", "2026-11-18T04:00:40.000Z"));
    expect(segments(later)[0]).not.toBe(segments(thinking)[0]);
    expect(segments(projectTurn(thinking.parts, "running", null, thinking, step("s2", next)))[0]).toBe(segments(thinking)[0]);
  });

  it("distinguishes pending queue, background execution, settling, waits and terminal states on call rows", () => {
    expect(projectCallStatus("pending").label).toBe("准备执行");
    expect(projectCallStatus("pending", false, true).label).toBe("后台运行中");
    expect(projectCallStatus("pending", false, true, "settling")).toEqual({ label: "后台结果收尾中", animated: false });
    expect(projectCallStatus("pending", true, true).animated).toBe(false);
    for (const status of ["failed", "cancelled", "completed"] as const) expect(projectCallStatus(status, false, true).animated).toBe(false);
  });
});
