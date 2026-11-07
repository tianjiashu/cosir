import { describe, expect, it } from "vitest";
import type { TransportMessagePart, TransportToolCallPart } from "@/lib/assistant/contract";
import { aggregateChanges, describeProcessProblems, projectTurn, visibleTurnBlock } from "@/lib/assistant/turn-layout";
import { agentMessagePart, outcome, toolCallPart } from "./transport-fixtures";
const text = (value: string, finalReply = false): TransportMessagePart => ({ type: "text", text: value, finalReply });
const call = (id: string, overrides: Partial<TransportToolCallPart> = {}) => toolCallPart(id, "read_file", { status: "completed", ...overrides });

describe("stable conversation projection", () => {
  it("途中来信切开工具组、随过程折叠，只有来信和最终回复时标题仍能展开", () => {
    const parts = [call("a"), agentMessagePart("5"), call("b"), text("结论", true)];
    const layout = projectTurn(parts, "settled", outcome("completed"));
    expect(layout.blocks.map((block) => block.kind)).toEqual(["activity", "agent", "activity", "narration"]);
    const letter = layout.blocks[1];
    expect(visibleTurnBlock(letter, false)).toBe(false);
    expect(visibleTurnBlock(letter, true)).toBe(true);
    expect(projectTurn([agentMessagePart("5"), text("结论", true)], "settled", outcome("completed")).process.items).toHaveLength(1);
    expect(projectTurn([agentMessagePart("5")], "running").intro).toBeNull();
  });

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
    const parts = [text("进展"), call("one"), text("回复", true)];
    expect(projectTurn(parts, "settled").hasFinalReply).toBe(false);
    expect(projectTurn(parts, "settled", outcome("cancelled")).defaultProcessOpen).toBe(true);
    expect(projectTurn(parts, "settled", outcome("failed")).answer).toHaveLength(2);
    const complete = projectTurn(parts, "settled", outcome("completed"));
    expect(complete.hasFinalReply).toBe(true);
    expect(complete.defaultProcessOpen).toBe(false);
    expect(complete.answer.map((item) => item.part.text)).toEqual(["回复"]);
    expect(projectTurn([text("进展"), call("one")], "settled", outcome("completed")).defaultProcessOpen).toBe(true);
  });
  it("wait stays out of work counts, but failures remain visible without renaming the interval", () => {
    const wait = call("wait", { toolName: "wait_messages", presentation: "quiet" });
    const before = projectTurn([wait, call("read")], "suspended");
    const after = projectTurn([{ ...wait, status: "failed" }, call("read")], "suspended");
    expect(before.blocks[0]).toMatchObject({ key: "interval:0", calls: [{ part: { toolCallId: "read" } }] });
    expect(after.blocks[0]).toMatchObject({ key: "interval:0" });
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
  });
  it("completed result warnings are counted per call, not per failed item", () => {
    const layout = projectTurn([
      call("terminal", { display_data: { kind: "terminal-result", exit_code: 1 } }),
      call("images", { display_data: { kind: "image-read", failures: ["a", "b"] } }),
      call("browser", { display_data: { kind: "browser-page", steps: [{ status: "skipped" }] } }),
      call("web", { display_data: { kind: "web-extract-urls", partial: true } }),
      call("mcp", { display_data: { kind: "mcp-tool-result", action: "list", servers: [{ error: "offline" }] } }),
    ], "settled");
    expect(layout.process.failedCount).toBe(0);
    expect(describeProcessProblems(layout.process)).toBeNull();
    expect(layout.process.verbCounts).toEqual([{ label: "读取文件", count: 5 }]);
  });
  it("unknown tools remain visible and do not acquire invented result semantics", () => {
    const layout = projectTurn([call("future", { toolName: "future_tool", status: "completed", display_data: { kind: "future-result" } })], "settled");
    expect(layout.process.verbCounts).toEqual([{ label: "future_tool", count: 1 }]);
  });
  it("keeps every file operation in order, including deletion then recreation", () => {
    const change = (id: string, status: string, insertions: number) => call(id, { display_data: { kind: "file-changes", changes: [{ path: "a", status, insertions }] } });
    const card = aggregateChanges([{ index: 0, part: change("delete", "deleted", 0) }, { index: 1, part: change("new", "added", 12) }]);
    expect(card.totals).toEqual({ files: 1, calls: 2 });
    expect(card.files[0].operations.map((op) => [op.toolCallId, op.change.status, op.change.insertions])).toEqual([["delete", "deleted", 0], ["new", "added", 12]]);
  });
  it("keeps artifacts and required decisions available independently of process folding", () => {
    const team = call("team", { awaitingUser: true, status: "pending", display_data: { kind: "agent-team-approval" } });
    const draft = call("draft", { display_data: { kind: "agent-configuration-draft" } });
    const layout = projectTurn([team, draft, text("回复", true)], "settled", outcome("completed"));
    expect(layout.defaultProcessOpen).toBe(false);
    expect(layout.attention).toHaveLength(1);
    expect(layout.artifacts.map((item) => item.kind)).toEqual(["agent-configuration-draft"]);
  });
});

it("reuses unchanged tool groups and file aggregation across prose versions, but refreshes changed calls", () => {
  const change = toolCallPart("write", "write_file", { status: "completed", display_data: { kind: "file-changes", changes: [{ path: "a.ts", status: "modified", insertions: 2 }] } });
  const first = projectTurn([change, { type: "text", text: "a" }], "running");
  const streamed = projectTurn([change, { type: "text", text: "ab" }], "running", null, first);
  expect(streamed.blocks[0]).toBe(first.blocks[0]);
  expect(streamed.artifacts[0]).toBe(first.artifacts[0]);
  expect(streamed.process.verbCounts).toBe(first.process.verbCounts);
  const failed = projectTurn([{ ...change, status: "failed" }, { type: "text", text: "ab" }], "settled", null, streamed);
  expect(failed.blocks[0]).not.toBe(first.blocks[0]);
  expect(failed.process.failedCount).toBe(1);
  const updated = projectTurn([{ ...change, display_data: { kind: "file-changes", changes: [{ path: "b.ts", status: "added" }] } }], "settled", null, failed);
  expect(updated.artifacts[0]).toMatchObject({ files: [{ path: "b.ts" }] });
});
