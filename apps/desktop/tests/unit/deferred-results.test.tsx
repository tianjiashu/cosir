import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
// 服务端渲染读 store 的初始状态；组件只经 useDeferredResults 读条目，这里换成可控的条目表。
const board = vi.hoisted(() => ({ entries: new Map<number, unknown[]>() }));
vi.mock("@/lib/task-overview/deferred-results-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/task-overview/deferred-results-store")>(),
  useDeferredResults: (taskId: number | null) => (taskId === null ? [] : board.entries.get(taskId) ?? []),
}));

import { DeferredResultActions } from "@/components/assistant-ui/tools/deferred-result-actions";
import { readToolArtifact } from "@/components/assistant-ui/tools/types";
import { DeferredResultsCard } from "@/components/task-overview/deferred-results-card";
import { formatElapsed } from "@/components/task-overview/deferred-result-row";
import type { DeferredResultEntry } from "@/lib/assistant/contract";
import { toToolCallPart } from "@/lib/assistant/converter";
import { parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/snapshot-validation";
import { backgroundToolCall } from "@/lib/assistant/tool-call-commands";
import { countRunning, useDeferredResultsStore } from "@/lib/task-overview/deferred-results-store";

import { transportState } from "./transport-fixtures";

function entry(toolCallId: string, overrides: Partial<DeferredResultEntry> = {}): DeferredResultEntry {
  return {
    toolCallId,
    kind: "terminal_wait",
    label: `terminal_wait:${toolCallId}`,
    mode: "background",
    status: "running",
    startedAt: "2026-10-28T08:00:00+00:00",
    endedAt: null,
    ...overrides,
  };
}

beforeEach(() => {
  useDeferredResultsStore.setState({ tasks: {} });
  board.entries.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe("snapshot deferred_results 与工具 part deferred", () => {
  it("deferred_results 是必填数组，条目按固定字段校验", () => {
    const withoutEntries: Record<string, unknown> = { ...transportState() };
    delete withoutEntries.deferred_results;
    expect(() => parseTransportState(withoutEntries)).toThrow(TransportSnapshotValidationError);

    const snapshot = transportState({
      deferred_results: [entry("a"), entry("b", { status: "completed", endedAt: "2026-10-28T08:01:00+00:00" })],
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
    expect(() => parseTransportState(transportState({ deferred_results: [entry("a"), entry("a")] })))
      .toThrow(/deferred_results\[1\]\.toolCallId/);
    expect(() => parseTransportState(transportState({ deferred_results: [entry("a", { mode: "sideways" as never })] })))
      .toThrow(/deferred_results\[0\]\.mode/);
  });

  it("converter 保留后台占位标记，并用后端短提示说明取消原因", () => {
    const pending = toToolCallPart({ type: "tool-call", toolCallId: "c1", toolName: "terminal_read", args: {}, status: "pending", deferred: true });
    expect(readToolArtifact((pending as { artifact?: unknown }).artifact).deferred).toBe(true);
    const interrupted = toToolCallPart({ type: "tool-call", toolCallId: "c2", toolName: "terminal_read", args: {}, status: "cancelled", error: "已中断", deferred: true });
    expect(readToolArtifact((interrupted as { artifact?: unknown }).artifact).error).toBe("已中断");
  });
});

describe("DeferredResultActions", () => {
  const artifact = (status: "pending" | "running" | "completed", deferred?: boolean) => readToolArtifact({
    backendStatus: status, presentation: {}, display_data: null, deferred,
  });

  it("后台占位显示「后台执行中」与取消", () => {
    const html = renderToStaticMarkup(<DeferredResultActions taskId={1} toolCallId="c1" artifact={artifact("pending", true)} />);
    expect(html).toContain("后台执行中");
    expect(html).toContain("取消");
  });

  it("前台等待只在看板里有前台运行条目时提供「转到后台」", () => {
    expect(renderToStaticMarkup(<DeferredResultActions taskId={1} toolCallId="c1" artifact={artifact("running")} />)).toBe("");
    board.entries.set(1, [entry("c1", { mode: "foreground" })]);
    expect(renderToStaticMarkup(<DeferredResultActions taskId={1} toolCallId="c1" artifact={artifact("running")} />)).toContain("转到后台");
  });

  it("结论到达后不再渲染操作行", () => {
    expect(renderToStaticMarkup(<DeferredResultActions taskId={1} toolCallId="c1" artifact={artifact("completed", true)} />)).toBe("");
  });
});

describe("后台任务卡", () => {
  it("没有条目时不渲染，运行中在前、已结束收成「N 已完成」", () => {
    expect(renderToStaticMarkup(<DeferredResultsCard taskId={1} />)).toBe("");
    board.entries.set(1, [
      entry("run", { label: "cd /work && make" }),
      entry("done", { status: "completed", endedAt: "2026-10-28T08:00:42+00:00" }),
    ]);
    const html = renderToStaticMarkup(<DeferredResultsCard taskId={1} />);
    expect(html).toContain("后台任务");
    expect(html).toContain("cd /work &amp;&amp; make");
    expect(html).toContain("1 已完成");
    expect(html).not.toContain("terminal_wait:done");
  });

  it("运行数与耗时格式", () => {
    expect(countRunning([entry("a"), entry("b", { status: "cancelled" })])).toBe(1);
    expect(formatElapsed(0, 42_000)).toBe("42s");
    expect(formatElapsed(0, 125_000)).toBe("2m 5s");
    expect(formatElapsed(0, 3_720_000)).toBe("1h 2m");
  });

  it("离开 Task 置空、删除 Task 丢弃桶", () => {
    const store = useDeferredResultsStore.getState();
    store.sync(1, [entry("a")]);
    store.sync(1, null);
    expect(useDeferredResultsStore.getState().tasks[1]).toBeNull();
    store.forget([1]);
    expect(1 in useDeferredResultsStore.getState().tasks).toBe(false);
  });
});

describe("backgroundToolCall", () => {
  it("POST 到按 Task 寻址的转后台接口，409 视为不可转", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(backgroundToolCall(3, "call/1")).resolves.toEqual({ kind: "detached" });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/tasks\/3\/tool-calls\/call%2F1\/background$/),
      expect.objectContaining({ method: "POST" }),
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 409 })));
    await expect(backgroundToolCall(3, "c")).resolves.toEqual({ kind: "not_detachable" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await expect(backgroundToolCall(3, "c")).resolves.toMatchObject({ kind: "failed" });
  });
});
