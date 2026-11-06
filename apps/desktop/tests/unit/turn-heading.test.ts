import { describe, expect, it } from "vitest";
import { projectTurn } from "@/lib/assistant/turn-layout";
import { projectTurnHeading } from "@/lib/assistant/turn-heading";
import { outcome, toolCallPart } from "./transport-fixtures";

const text = { type: "text" as const, text: "回复" };
const call = toolCallPart("c", "read_file", { status: "completed" });
describe("single turn heading", () => {
  it("completed status is implicit; duration and real process determine whether a heading exists", () => {
    const done = outcome("completed");
    const plain = projectTurn([text], "settled", done);
    expect(projectTurnHeading(plain, done, null)).toBeNull();
    expect(projectTurnHeading(plain, done, 129000)).toMatchObject({ label: "用时 2分钟9秒", expandable: false });
    const process = projectTurn([call, text], "settled", done);
    expect(projectTurnHeading(process, done, 129000)).toMatchObject({ label: "用时 2分钟9秒", expandable: true });
    expect(projectTurnHeading(process, done, null)).toMatchObject({ label: "查看过程", expandable: true });
    expect(projectTurnHeading(plain, done, 0)?.label).toBe("用时 0秒");
  });
  it("terminal outcomes take priority over local stop feedback; child cancellation cannot cancel its parent", () => {
    for (const [status, label] of [["cancelled", "已停止"], ["failed", "运行失败"]] as const) {
      const end = outcome(status);
      expect(projectTurnHeading(projectTurn([call], "settled", end), end, 8000, true)?.label).toBe(`${label} · 用时 8秒`);
    }
    const done = outcome("completed");
    const child = { ...call, status: "cancelled" as const };
    expect(projectTurnHeading(projectTurn([child, text], "settled", done), done, 8000, true)).toMatchObject({ label: "用时 8秒", notice: null, failed: false });
    const interrupted = outcome("cancelled", { endReason: "runtime_restarted" });
    expect(projectTurnHeading(projectTurn([], "settled", interrupted), interrupted, null)?.label).toBe("运行已中断 · 后端已重启");
  });
  it("keeps tool warnings discoverable without changing the completed outcome", () => {
    const done = outcome("completed");
    const failedCall = { ...call, status: "failed" as const };
    expect(projectTurnHeading(projectTurn([failedCall, text], "settled", done), done, null)).toMatchObject({ notice: "有异常记录", failed: false });
  });
  it("does not create headings for intro states or invent an outcome for historical process", () => {
    expect(projectTurnHeading(projectTurn([], "running"), null, 8000)).toBeNull();
    expect(projectTurnHeading(projectTurn([{ type: "text", text: "" }], "running"), null, 8000)).toBeNull();
    expect(projectTurnHeading(projectTurn([call], "settled"), null, null)?.label).toBe("查看过程");
    expect(projectTurnHeading(projectTurn([text], "suspended"), null, null)).toBeNull();
    expect(projectTurnHeading(projectTurn([text], "running"), null, null, true)).toBeNull();
  });
});
