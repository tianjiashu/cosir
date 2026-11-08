import { describe, expect, it } from "vitest";
import { projectTurn } from "@/lib/assistant/turn/turn-layout";
import { projectTurnHeading } from "@/lib/assistant/turn/turn-heading";
import { outcome, toolCallPart } from "./transport-fixtures";

const text = { type: "text" as const, finalReply: true, text: "回复" };
const call = toolCallPart("c", "read_file", { status: "completed" });
describe("single turn heading", () => {
  it("completed replies require real process, regardless of duration", () => {
    const done = outcome("completed", { durationMs: 8000 });
    const plain = projectTurn([text], "settled", done);
    for (const duration of [0, 129000]) expect(projectTurnHeading(plain, { ...done, durationMs: duration })).toBeNull();
    const process = projectTurn([call, text], "settled", done);
    expect(projectTurnHeading(process, { ...done, durationMs: 129000 })).toMatchObject({ label: "用时 2分钟9秒", expandable: true });
    expect(projectTurnHeading(process, done, 999000)).toMatchObject({ label: "用时 8秒", expandable: true });
    expect(projectTurnHeading(process, { ...done, durationMs: 0 })?.label).toBe("用时 0秒");
  });
  it("uses visible process, including narration and exceptional quiet calls, rather than tool count", () => {
    const done = outcome("completed", { durationMs: 8000 });
    const quiet = { ...call, presentation: "quiet" as const };
    expect(projectTurnHeading(projectTurn([quiet, text], "settled", done), done, 8000)).toBeNull();
    expect(projectTurnHeading(projectTurn([{ ...text, text: "进展", finalReply: false }, text], "settled", done), done, null))
      .toMatchObject({ label: "用时 8秒", expandable: true });
    for (const part of [{ ...quiet, status: "failed" as const }, { ...quiet, awaitingUser: true }]) {
      expect(projectTurnHeading(projectTurn([part, text], "settled", done), done, 8000)?.expandable).toBe(true);
    }
    // 截断文本没有 finalReply，仍是可查看的内容，不能被当作普通完成回复。
    const truncated = outcome("completed", { endReason: "length" });
    expect(projectTurnHeading(projectTurn([{ ...text, finalReply: false }], "settled", truncated), truncated, 8000)?.expandable).toBe(true);
  });
  // 目的：运行中只有正文时正文本身就是反馈；潜在缺陷：未定性的正文被当成过程，纯回复顶着计时标题、完成时标题消失闪一下，点标题还会藏起正在生成的回复。
  it("live and suspended turns show the timer only above real process", () => {
    const streaming = { ...text, finalReply: false };
    for (const phase of ["running", "suspended"] as const) {
      expect(projectTurnHeading(projectTurn([streaming], phase), null, 8000)).toBeNull();
      expect(projectTurnHeading(projectTurn([call, streaming], phase), null, 8000)).toMatchObject({ label: "用时 8秒", expandable: true });
    }
  });
  // 目的：继承历史没有本地 Run、无从定性；潜在缺陷：每条继承的纯文字回复都顶着「查看过程」，点开合会把正文藏起来。
  it("inherited prose has no heading; inherited tools keep the process entry", () => {
    const inherited = { ...text, finalReply: false };
    expect(projectTurnHeading(projectTurn([inherited], "settled"), null, null)).toBeNull();
    expect(projectTurnHeading(projectTurn([call, inherited], "settled"), null, null)).toMatchObject({ label: "查看过程", expandable: true });
  });
  it("terminal outcomes remain visible; child cancellation cannot cancel its parent", () => {
    for (const [status, label] of [["cancelled", "已停止"], ["failed", "运行失败"]] as const) {
      const end = outcome(status, { durationMs: 8000 });
      expect(projectTurnHeading(projectTurn([call], "settled", end), end, 8000)?.label).toBe(`${label} · 用时 8秒`);
      expect(projectTurnHeading(projectTurn([], "settled", end), end, null)).toMatchObject({ label: `${label} · 用时 8秒`, expandable: false });
    }
    const done = outcome("completed", { durationMs: 8000 });
    const child = { ...call, status: "cancelled" as const };
    expect(projectTurnHeading(projectTurn([child, text], "settled", done), done, 8000)).toMatchObject({ label: "用时 8秒", failed: false });
    const interrupted = outcome("cancelled", { endReason: "runtime_restarted" });
    expect(projectTurnHeading(projectTurn([], "settled", interrupted), interrupted, null)?.label).toBe("运行已中断 · 后端已重启 · 用时 0秒");
  });
  it("reports only the Run outcome: tool failures stay in the process", () => {
    const done = outcome("completed", { durationMs: 8000 });
    const failedCall = { ...call, status: "failed" as const };
    expect(projectTurnHeading(projectTurn([failedCall, text], "settled", done), done, null)).toEqual({ label: "用时 8秒", expandable: true, failed: false });
  });
  it("does not create headings for intro states or invent an outcome for historical process", () => {
    expect(projectTurnHeading(projectTurn([], "running"), null, 8000)).toBeNull();
    expect(projectTurnHeading(projectTurn([{ type: "text", finalReply: true, text: "" }], "running"), null, 8000)).toBeNull();
    expect(projectTurnHeading(projectTurn([call], "settled"), null, null)?.label).toBe("查看过程");
    expect(projectTurnHeading(projectTurn([text], "suspended"), null, null)).toBeNull();
    expect(projectTurnHeading(projectTurn([text], "running"), null, null)).toBeNull();
  });
});
