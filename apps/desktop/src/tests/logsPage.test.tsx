// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LogsPage } from "@/pages/logs/LogsPage";
import { fetchLogsByTrace, fetchRecentLogs } from "@/services/logs";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";
import { useTaskStore } from "@/stores/taskStore";
import { makeTask } from "@/tests/test-utils/factories";

vi.mock("@/services/logs", () => ({
  fetchRecentLogs: vi.fn(),
  fetchLogsByTrace: vi.fn(),
}));

vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

const mockedFetchRecentLogs = vi.mocked(fetchRecentLogs);
const mockedFetchLogsByTrace = vi.mocked(fetchLogsByTrace);

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  mockedFetchRecentLogs.mockReset();
  mockedFetchLogsByTrace.mockReset();
  useTaskStore.getState().clearTasks();
  useConversationTraceStore.getState().resetConversationTraces();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

describe("LogsPage — 日志页面", () => {
  it("首次进入页面会查询最近日志并渲染纯文本、结构化字段和 stack", async () => {
    mockedFetchRecentLogs.mockResolvedValue({
      text: "2026-07-15 ERROR tool_call_failed",
      entries: [
        {
          ts: "2026-07-15T10:00:00.000Z",
          level: "ERROR",
          logger: "coding_agent.backend",
          trace_id: "trace-1",
          caller: "app.tools:Tool.run:42",
          event: "tool_call_failed",
          msg: "工具失败",
          data: {},
          error: { type: "RuntimeError", message: "boom", stack: "Traceback" },
          truncated: false,
        },
      ],
    });

    await renderLogsPage();

    expect(mockedFetchRecentLogs).toHaveBeenCalledWith({ level: undefined, limit: 200 });
    expect(container.textContent).toContain("2026-07-15 ERROR tool_call_failed");
    expect(container.textContent).toContain("tool_call_failed");
    expect(container.textContent).toContain("工具失败");
    expect(container.textContent).toContain("Traceback");
  });

  it("输入 trace_id 后点击查询会调用 trace 查询接口", async () => {
    mockedFetchRecentLogs.mockResolvedValue({ entries: [], text: "" });
    mockedFetchLogsByTrace.mockResolvedValue({
      entries: [],
      text: "trace logs",
    });

    await renderLogsPage();
    const input = container.querySelector('input[aria-label="trace_id"]') as HTMLInputElement;
    await act(async () => {
      setInputValue(input, "trace-2");
    });
    const button = Array.from(container.querySelectorAll("button")).find((item) =>
      item.textContent?.includes("查询"),
    );

    await act(async () => {
      button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(mockedFetchLogsByTrace).toHaveBeenCalledWith({
      trace_id: "trace-2",
      level: undefined,
      limit: 200,
    });
    expect(container.textContent).toContain("trace logs");
  });

  it("当前任务存在对话 trace 时首次进入会直接查询该 trace", async () => {
    useTaskStore.getState().addTask(makeTask("task-1"));
    useConversationTraceStore.getState().recordTrace({
      traceId: "1234567890abcdef1234567890abcdef",
      taskId: "task-1",
      approvalId: "",
      operation: "task_stream",
      method: "GET",
      path: "/tasks/task-1/stream",
    });
    mockedFetchLogsByTrace.mockResolvedValue({ entries: [], text: "current trace logs" });

    await renderLogsPage();

    expect(mockedFetchRecentLogs).not.toHaveBeenCalled();
    expect(mockedFetchLogsByTrace).toHaveBeenCalledWith({
      trace_id: "1234567890abcdef1234567890abcdef",
      level: undefined,
      limit: 200,
    });
    expect(container.textContent).toContain("current trace logs");
  });

  it("日志页打开后当前任务产生 trace 时会自动查询该 trace", async () => {
    useTaskStore.getState().addTask(makeTask("task-1"));
    mockedFetchRecentLogs.mockResolvedValue({ entries: [], text: "recent logs" });
    mockedFetchLogsByTrace.mockResolvedValue({ entries: [], text: "late trace logs" });

    await renderLogsPage();
    await act(async () => {
      useConversationTraceStore.getState().recordTrace({
        traceId: "abcdef1234567890abcdef1234567890",
        taskId: "task-1",
        approvalId: "",
        operation: "task_stream",
        method: "GET",
        path: "/tasks/task-1/stream",
      });
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(mockedFetchLogsByTrace).toHaveBeenCalledWith({
      trace_id: "abcdef1234567890abcdef1234567890",
      level: undefined,
      limit: 200,
    });
    expect(container.textContent).toContain("late trace logs");
  });

  it("后到的 stream trace 会替换自动填入的普通端点 trace", async () => {
    useTaskStore.getState().addTask(makeTask("task-1"));
    useConversationTraceStore.getState().recordTrace({
      traceId: "11111111111111111111111111111111",
      taskId: "task-1",
      approvalId: "",
      operation: "task_create",
      method: "POST",
      path: "/tasks",
    });
    mockedFetchLogsByTrace.mockResolvedValueOnce({ entries: [], text: "create trace logs" });
    mockedFetchLogsByTrace.mockResolvedValueOnce({ entries: [], text: "stream trace logs" });

    await renderLogsPage();
    await act(async () => {
      useConversationTraceStore.getState().recordTrace({
        traceId: "22222222222222222222222222222222",
        taskId: "task-1",
        approvalId: "",
        operation: "task_stream",
        method: "GET",
        path: "/tasks/task-1/stream",
      });
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(mockedFetchLogsByTrace).toHaveBeenLastCalledWith({
      trace_id: "22222222222222222222222222222222",
      level: undefined,
      limit: 200,
    });
    expect((container.querySelector('input[aria-label="trace_id"]') as HTMLInputElement).value).toBe(
      "22222222222222222222222222222222",
    );
    expect(container.textContent).toContain("stream trace logs");
  });

  it("查询失败时展示后端错误文案", async () => {
    mockedFetchRecentLogs.mockRejectedValue(new Error("日志查询失败: invalid level"));

    await renderLogsPage();

    expect(container.textContent).toContain("日志查询失败: invalid level");
  });

  it("空结果时展示空态", async () => {
    mockedFetchRecentLogs.mockResolvedValue({ entries: [], text: "" });

    await renderLogsPage();

    expect(container.textContent).toContain("暂无日志内容");
    expect(container.textContent).toContain("(empty)");
  });
});

async function renderLogsPage(): Promise<void> {
  await act(async () => {
    root.render(<LogsPage onBack={vi.fn()} />);
  });
  await act(async () => {
    await Promise.resolve();
  });
}

function setInputValue(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}
