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

describe("LogsPage 鈥?鏃ュ織椤甸潰", () => {
  it("棣栨杩涘叆椤甸潰浼氭煡璇㈡渶杩戞棩蹇楀苟娓叉煋绾枃鏈€佺粨鏋勫寲瀛楁鍜?stack", async () => {
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
          msg: "宸ュ叿澶辫触",
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
    expect(container.textContent).toContain("宸ュ叿澶辫触");
    expect(container.textContent).toContain("Traceback");
  });

  it("杈撳叆 trace_id 鍚庣偣鍑绘煡璇細璋冪敤 trace 鏌ヨ鎺ュ彛", async () => {
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

  it("褰撳墠浠诲姟瀛樺湪瀵硅瘽 trace 鏃堕娆¤繘鍏ヤ細鐩存帴鏌ヨ璇?trace", async () => {
    useTaskStore.getState().addTask(makeTask("task-1"));
    useConversationTraceStore.getState().recordTrace({
      traceId: "1234567890abcdef1234567890abcdef",
      taskId: "task-1",
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

  it("鏃ュ織椤垫墦寮€鍚庡綋鍓嶄换鍔′骇鐢?trace 鏃朵細鑷姩鏌ヨ璇?trace", async () => {
    useTaskStore.getState().addTask(makeTask("task-1"));
    mockedFetchRecentLogs.mockResolvedValue({ entries: [], text: "recent logs" });
    mockedFetchLogsByTrace.mockResolvedValue({ entries: [], text: "late trace logs" });

    await renderLogsPage();
    await act(async () => {
      useConversationTraceStore.getState().recordTrace({
        traceId: "abcdef1234567890abcdef1234567890",
        taskId: "task-1",
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

  it("鍚庡埌鐨?stream trace 浼氭浛鎹㈣嚜鍔ㄥ～鍏ョ殑鏅€氱鐐?trace", async () => {
    useTaskStore.getState().addTask(makeTask("task-1"));
    useConversationTraceStore.getState().recordTrace({
      traceId: "11111111111111111111111111111111",
      taskId: "task-1",
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

  it("shows backend error text when query fails", async () => {
    mockedFetchRecentLogs.mockRejectedValue(new Error("鏃ュ織鏌ヨ澶辫触: invalid level"));

    await renderLogsPage();

    expect(container.textContent).toContain("鏃ュ織鏌ヨ澶辫触: invalid level");
  });

  it("shows empty state for empty results", async () => {
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
