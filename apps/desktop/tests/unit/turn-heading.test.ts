import { describe, expect, it } from "vitest";
import { projectTurn } from "@/lib/assistant/turn-layout";
import { projectTurnHeading } from "@/lib/assistant/turn-heading";
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
  it("keeps live and suspended duration feedback until normal completion", () => {
    for (const phase of ["running", "suspended"] as const) {
      expect(projectTurnHeading(projectTurn([text], phase), null, 8000)).toMatchObject({ label: "用时 8秒", expandable: false });
    }
  });
  it("terminal outcomes remain visible; child cancellation cannot cancel its parent", () => {
    for (const [status, label] of [["cancelled", "已停止"], ["failed", "运行失败"]] as const) {
      const end = outcome(status, { durationMs: 8000 });
      expect(projectTurnHeading(projectTurn([call], "settled", end), end, 8000)?.label).toBe(`${label} · 用时 8秒`);
      expect(projectTurnHeading(projectTurn([], "settled", end), end, null)).toMatchObject({ label: `${label} · 用时 8秒`, expandable: false });
    }
    const done = outcome("completed", { durationMs: 8000 });
    const child = { ...call, status: "cancelled" as const };
    expect(projectTurnHeading(projectTurn([child, text], "settled", done), done, 8000)).toMatchObject({ label: "用时 8秒", notice: null, failed: false });
    const interrupted = outcome("cancelled", { endReason: "runtime_restarted" });
    expect(projectTurnHeading(projectTurn([], "settled", interrupted), interrupted, null)?.label).toBe("运行已中断 · 后端已重启 · 用时 0秒");
  });
  it("keeps tool warnings discoverable without changing the completed outcome", () => {
    const done = outcome("completed", { durationMs: 8000 });
    const failedCall = { ...call, status: "failed" as const };
    expect(projectTurnHeading(projectTurn([failedCall, text], "settled", done), done, null)).toMatchObject({ notice: "有异常记录", failed: false });
  });
  it("does not create headings for intro states or invent an outcome for historical process", () => {
    expect(projectTurnHeading(projectTurn([], "running"), null, 8000)).toBeNull();
    expect(projectTurnHeading(projectTurn([{ type: "text", finalReply: true, text: "" }], "running"), null, 8000)).toBeNull();
    expect(projectTurnHeading(projectTurn([call], "settled"), null, null)?.label).toBe("查看过程");
    expect(projectTurnHeading(projectTurn([text], "suspended"), null, null)).toBeNull();
    expect(projectTurnHeading(projectTurn([text], "running"), null, null)).toBeNull();
  });
});
