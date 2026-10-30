import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
// 服务端渲染读 store 的初始状态；组件只经 useBackgroundTasks / useForegroundWaits 读，这里换成可控的表。
const board = vi.hoisted(() => ({ entries: new Map<number, unknown[]>(), waits: new Map<number, string[]>(), parts: new Map<string, unknown>() }));
vi.mock("@/lib/task-overview/background-tasks-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/task-overview/background-tasks-store")>(),
  useBackgroundTasks: (taskId: number | null) => (taskId === null ? [] : board.entries.get(taskId) ?? []),
  useForegroundWaits: (taskId: number | null) => (taskId === null ? [] : board.waits.get(taskId) ?? []),
  useBackgroundTaskPart: (_taskId: number, toolCallId: string) => board.parts.get(toolCallId) ?? null,
}));

import { ForegroundWaitAction } from "@/components/assistant-ui/tools/foreground-wait-action";
import { readToolArtifact } from "@/components/assistant-ui/tools/types";
import { BackgroundTasksCard } from "@/components/task-overview/background-tasks-card";
import { formatElapsed } from "@/components/task-overview/background-task-row";
import type { BackgroundTaskEntry, TransportToolCallPart } from "@/lib/assistant/contract";
import { toToolCallPart } from "@/lib/assistant/converter";
import { parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/snapshot-validation";
import { backgroundToolCall } from "@/lib/assistant/task-commands";
import { countRunning, useBackgroundTasksStore } from "@/lib/task-overview/background-tasks-store";

import { transportState } from "./transport-fixtures";

function entry(toolCallId: string, overrides: Partial<BackgroundTaskEntry> = {}): BackgroundTaskEntry {
  return {
    toolCallId,
    kind: "terminal_command",
    label: `terminal_command:${toolCallId}`,
    status: "running",
    startedAt: "2026-10-28T08:00:00+00:00",
    endedAt: null,
    ...overrides,
  };
}

function terminalPart(toolCallId: string, overrides: Partial<TransportToolCallPart> = {}): TransportToolCallPart {
  return {
    type: "tool-call",
    toolCallId,
    toolName: "execute_terminal",
    args: { command: "make watch" },
    status: "pending",
    presentation: { surface: "standalone", expandable: true, expand_layout: "terminal" },
    display_data: { kind: "terminal-result", command: "make watch", workdir: "/work" },
    deferred: true,
    ...overrides,
  };
}

function withParts(...parts: TransportToolCallPart[]) {
  return transportState({ messages: [{ id: "m1", role: "assistant", runId: 1, outcome: null, parts }] as never });
}

beforeEach(() => {
  useBackgroundTasksStore.setState({ tasks: {}, foregroundWaits: {}, parts: {} });
  board.entries.clear();
  board.waits.clear();
  board.parts.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe("snapshot background_tasks 与工具 part background", () => {
  it("background_tasks 是必填数组，条目按固定字段校验", () => {
    const withoutEntries: Record<string, unknown> = { ...transportState() };
    delete withoutEntries.background_tasks;
    expect(() => parseTransportState(withoutEntries)).toThrow(TransportSnapshotValidationError);

    const snapshot = transportState({
      background_tasks: [entry("a"), entry("b", { status: "completed", endedAt: "2026-10-28T08:01:00+00:00" })],
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
    expect(() => parseTransportState(transportState({ background_tasks: [entry("a"), entry("a")] })))
      .toThrow(/background_tasks\[1\]\.toolCallId/);
    expect(() => parseTransportState(transportState({ background_tasks: [{ ...entry("a"), mode: "background" } as never] })))
      .toThrow(/background_tasks\[0\]/);
  });

  it("foreground_waits 是必填的唯一 id 数组", () => {
    const withoutWaits: Record<string, unknown> = { ...transportState() };
    delete withoutWaits.foreground_waits;
    expect(() => parseTransportState(withoutWaits)).toThrow(TransportSnapshotValidationError);
    const snapshot = transportState({ foreground_waits: ["c1"] });
    expect(parseTransportState(snapshot)).toBe(snapshot);
    expect(() => parseTransportState(transportState({ foreground_waits: ["c1", "c1"] })))
      .toThrow(/foreground_waits\[1\]/);
  });

  it("converter 把 part 的 deferred 交给卡片，作为所有工具共用的「在后台」事实", () => {
    const placeholder = toToolCallPart(terminalPart("c1"));
    expect(readToolArtifact((placeholder as { artifact?: unknown }).artifact).deferred).toBe(true);
    const foreground = toToolCallPart(terminalPart("c2", { deferred: undefined, status: "running" }));
    expect(readToolArtifact((foreground as { artifact?: unknown }).artifact).deferred).toBe(false);
  });

  it("converter 用后端短提示说明后台占位的取消原因", () => {
    const interrupted = toToolCallPart({ type: "tool-call", toolCallId: "c2", toolName: "terminal_read", args: {}, status: "cancelled", error: "已中断", deferred: true });
    expect(readToolArtifact((interrupted as { artifact?: unknown }).artifact).error).toBe("已中断");
  });
});

describe("ForegroundWaitAction", () => {
  it("只在 foreground_waits 列出该调用时提供「转到后台」", () => {
    expect(renderToStaticMarkup(<ForegroundWaitAction taskId={1} toolCallId="c1" />)).toBe("");
    board.waits.set(1, ["c1"]);
    expect(renderToStaticMarkup(<ForegroundWaitAction taskId={1} toolCallId="c1" />)).toContain("转到后台");
  });
});

describe("后台任务卡", () => {
  it("没有条目时不渲染，运行中在前、已结束收成「N 已完成」", () => {
    expect(renderToStaticMarkup(<BackgroundTasksCard taskId={1} />)).toBe("");
    board.entries.set(1, [
      entry("run", { label: "cd /work && make" }),
      entry("done", { status: "completed", endedAt: "2026-10-28T08:00:42+00:00" }),
    ]);
    const html = renderToStaticMarkup(<BackgroundTasksCard taskId={1} />);
    expect(html).toContain("后台任务");
    expect(html).toContain("cd /work &amp;&amp; make");
    expect(html).toContain("1 已完成");
    expect(html).not.toContain("terminal_command:done");
  });

  it("运行数与耗时格式", () => {
    expect(countRunning([entry("a"), entry("b", { status: "cancelled" })])).toBe(1);
    expect(formatElapsed(0, 42_000)).toBe("42s");
    expect(formatElapsed(0, 125_000)).toBe("2m 5s");
    expect(formatElapsed(0, 3_720_000)).toBe("1h 2m");
  });

  it("离开 Task 置空、删除 Task 丢弃桶", () => {
    const store = useBackgroundTasksStore.getState();
    store.sync(1, { ...withParts(terminalPart("a")), background_tasks: [entry("a")], foreground_waits: ["c1"] });
    store.sync(1, null);
    expect(useBackgroundTasksStore.getState().tasks[1]).toBeNull();
    expect(useBackgroundTasksStore.getState().foregroundWaits[1]).toBeNull();
    expect(useBackgroundTasksStore.getState().parts[1]).toBeNull();
    store.forget([1]);
    expect(1 in useBackgroundTasksStore.getState().tasks).toBe(false);
    expect(1 in useBackgroundTasksStore.getState().foregroundWaits).toBe(false);
    expect(1 in useBackgroundTasksStore.getState().parts).toBe(false);
  });

  it("按条目 id 镜像同一个 snapshot 工具 part，part 未变时表的引用不变", () => {
    const store = useBackgroundTasksStore.getState();
    const part = terminalPart("a");
    const first = { ...withParts(part, terminalPart("other")), background_tasks: [entry("a")] };
    store.sync(1, first);
    const parts = useBackgroundTasksStore.getState().parts[1];
    expect(parts).toEqual({ a: part });
    expect(parts?.a).toBe(part);

    // 无关帧（新消息数组、同一 part 引用）不换表，订阅方不重渲染。
    store.sync(1, { ...first, messages: [...first.messages] });
    expect(useBackgroundTasksStore.getState().parts[1]).toBe(parts);

    // part 变了（如输出追加）就换成新的 part。
    const updated = { ...part, status: "completed" };
    store.sync(1, { ...withParts(updated), background_tasks: [entry("a")] });
    expect(useBackgroundTasksStore.getState().parts[1]?.a).toBe(updated);

    // 那一轮已不在对话里：条目还在，part 缺失。
    store.sync(1, { ...withParts(), background_tasks: [entry("a")] });
    expect(useBackgroundTasksStore.getState().parts[1]).toEqual({});
  });

  it("展开即对话里的同一张卡片：运行中默认展开，停止只在外壳上", () => {
    board.entries.set(1, [entry("a", { label: "terminal_command:make watch" })]);
    board.parts.set("a", terminalPart("a"));
    const html = renderToStaticMarkup(<BackgroundTasksCard taskId={1} />);
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain('data-tool-call-id="a"');
    expect(html).toContain("后台运行中");
    expect(html).toContain("在对话中定位");
    expect(html).not.toContain("停止终端命令");
    expect(html).not.toContain("转到后台");
  });

  it("对应 part 未加载（不在已加载的历史里）时只剩外壳、不可展开，仍可定位", () => {
    board.entries.set(1, [entry("a")]);
    const html = renderToStaticMarkup(<BackgroundTasksCard taskId={1} />);
    const row = html.slice(html.indexOf('data-background-task="a"'));
    expect(row).toContain("terminal_command:a");
    expect(row).not.toContain('aria-expanded="');
    expect(html).not.toContain("data-tool-call-id");
    // 定位由对话列表按需往上翻页，不依赖卡片已加载。
    expect(html).toContain("在对话中定位");
  });
});

describe("backgroundToolCall", () => {
  // http client 按 runtime config 解析后端地址，它读 window；单测跑在 Node 环境，给一个空 window。
  beforeEach(() => vi.stubGlobal("window", {}));
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
