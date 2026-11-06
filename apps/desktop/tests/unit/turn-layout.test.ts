import { describe, expect, it } from "vitest";
import type { TransportMessagePart, TransportToolCallPart } from "@/lib/assistant/contract";
import { aggregateChanges, projectTurn } from "@/lib/assistant/turn-layout";
import { outcome, toolCallPart } from "./transport-fixtures";
const text = (value: string): TransportMessagePart => ({ type: "text", text: value });
const call = (id: string, overrides: Partial<TransportToolCallPart> = {}) => toolCallPart(id, "read_file", { status: "completed", ...overrides });

describe("stable conversation projection", () => {
  it("only the active pre-output reply has compact chrome; whitespace and reasoning stay distinct from tools", () => {
    expect(projectTurn([], "running").intro).toBe("waiting");
    expect(projectTurn([text(" \n")], "running").blocks).toEqual([]);
    expect(projectTurn([text("答")], "running").intro).toBeNull();
    expect(projectTurn([call("first")], "running").intro).toBeNull();
    expect(projectTurn([call("wait", { toolName: "wait_messages", presentation: "quiet" })], "running").intro).toBeNull();
    for (const phase of ["settled", "suspended"] as const) expect(projectTurn([], phase).intro).toBeNull();
    for (const status of ["cancelled", "failed", "completed"] as const) expect(projectTurn([], "running", outcome(status)).intro).toBeNull();
  });
  it("appending a tool leaves existing prose in the same ordered slot", () => {
    const parts = [text("先读取"), call("one"), text("继续核对")];
    const before = projectTurn(parts, "running");
    const after = projectTurn([...parts, call("two", { status: "running" })], "running");
    expect(after.blocks.slice(0, 3)).toEqual(before.blocks);
    expect(after.answer.map((item) => item.part.text)).toEqual(["先读取", "继续核对"]);
    expect(after.defaultProcessOpen).toBe(true);
  });
  it("only an explicit completed outcome folds the earlier prose; empty tail never hides all text", () => {
    const parts = [text("进展"), call("one"), text("回复")];
    expect(projectTurn(parts, "settled").replyStart).toBe(0);
    expect(projectTurn(parts, "settled", outcome("cancelled")).defaultProcessOpen).toBe(true);
    expect(projectTurn(parts, "settled", outcome("failed")).answer).toHaveLength(2);
    const complete = projectTurn(parts, "settled", outcome("completed"));
    expect(complete.replyStart).toBe(2);
    expect(complete.defaultProcessOpen).toBe(false);
    expect(complete.answer.map((item) => item.part.text)).toEqual(["回复"]);
    expect(projectTurn([text("进展"), call("one")], "settled", outcome("completed")).defaultProcessOpen).toBe(true);
  });
  it("wait stays out of work counts, but failures remain visible without renaming the interval", () => {
    const wait = call("wait", { toolName: "wait_messages", presentation: "quiet" });
    const before = projectTurn([wait, call("read")], "suspended");
    const after = projectTurn([{ ...wait, status: "failed" }, call("read")], "suspended");
    expect(before.blocks[0]).toMatchObject({ key: "wait", calls: [{ part: { toolCallId: "read" } }] });
    expect(after.blocks[0]).toMatchObject({ key: "wait" });
    expect(after.process.verbCounts).toEqual([{ label: "读取文件", count: 1 }]);
    expect(after.process.failedCount).toBe(1);
    expect(projectTurn([wait], "suspended").process.items).toEqual([]);
  });
  it("body and reasoning delimit tool groups; background/decision facts stay independent", () => {
    const pending = call("decision", { awaitingUser: true, status: "pending" });
    const parts = [call("a"), text("说明"), pending, call("b")];
    const layout = projectTurn(parts, "suspended");
    expect(layout.blocks.map((item) => item.kind)).toEqual(["activity", "narration", "activity"]);
    expect(layout.attention).toEqual([{ index: 2, part: pending }]);
    expect(layout.process.currentAction).toBeNull();
  });
  it("completed result warnings are counted per call, not per failed item", () => {
    const layout = projectTurn([
      call("terminal", { display_data: { kind: "terminal-result", exit_code: 1 } }),
      call("images", { display_data: { kind: "image-read", failures: ["a", "b"] } }),
      call("browser", { display_data: { kind: "browser-page", steps: [{ status: "skipped" }] } }),
      call("web", { display_data: { kind: "web-extract-urls", partial: true } }),
      call("mcp", { display_data: { kind: "mcp-tool-result", action: "list", servers: [{ error: "offline" }] } }),
    ], "settled");
    expect(layout.process.warningCount).toBe(5);
    expect(layout.process.failedCount).toBe(0);
    expect(layout.process.verbCounts).toEqual([{ label: "读取文件", count: 5 }]);
  });
  it("unknown tools remain visible and do not acquire invented result semantics", () => {
    const layout = projectTurn([call("future", { toolName: "future_tool", status: "unknown", display_data: { kind: "future-result" } })], "settled");
    expect(layout.process.verbCounts).toEqual([{ label: "future_tool", count: 1 }]);
    expect(layout.process.warningCount).toBe(1);
  });
  it("keeps every file operation in order, including deletion then recreation", () => {
    const change = (id: string, status: string, insertions: number) => call(id, { display_data: { kind: "file-changes", changes: [{ path: "a", status, insertions }] } });
    const card = aggregateChanges([{ index: 0, part: change("delete", "deleted", 0) }, { index: 1, part: change("new", "added", 12) }]);
    expect(card.totals).toEqual({ files: 1, calls: 2 });
    expect(card.files[0].operations.map((op) => [op.toolCallId, op.status, op.additions])).toEqual([["delete", "deleted", 0], ["new", "added", 12]]);
  });
  it("keeps artifacts and required decisions available independently of process folding", () => {
    const team = call("team", { awaitingUser: true, status: "pending", display_data: { kind: "agent-team-approval" } });
    const draft = call("draft", { display_data: { kind: "agent-configuration-draft" } });
    const layout = projectTurn([team, draft, text("回复")], "settled", outcome("completed"));
    expect(layout.defaultProcessOpen).toBe(false);
    expect(layout.attention).toHaveLength(1);
    expect(layout.artifacts.map((item) => item.kind)).toEqual(["agent-configuration-draft"]);
  });
});
