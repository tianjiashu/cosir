// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationTraceBlock } from "@/components/right-panel/ConversationTraceBlock";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";
import { useTaskStore } from "@/stores/taskStore";
import { makeTask } from "@/tests/test-utils/factories";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  useTaskStore.getState().clearTasks();
  useConversationTraceStore.getState().resetConversationTraces();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

describe("ConversationTraceBlock", () => {
  it("renders empty state when active task has no trace", async () => {
    useTaskStore.getState().addTask(makeTask("task-1"));

    await renderBlock();

    expect(container.textContent).toContain("当前对话尚未产生 trace");
  });

  it("renders stream trace and invokes onOpenLogs", async () => {
    const onOpenLogs = vi.fn();
    useTaskStore.getState().addTask(makeTask("task-1"));
    useConversationTraceStore.getState().recordTrace({
      traceId: "11111111111111111111111111111111",
      taskId: "task-1",
      operation: "task_stream",
      method: "GET",
      path: "/tasks/task-1/stream",
    });
    useConversationTraceStore.getState().recordTrace({
      traceId: "22222222222222222222222222222222",
      taskId: "task-1",
      operation: "task_turns",
      method: "GET",
      path: "/tasks/task-1/turns",
    });

    await renderBlock(onOpenLogs);
    const button = Array.from(container.querySelectorAll("button")).find((item) =>
      item.textContent?.includes("查看日志"),
    );
    await act(async () => {
      button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("stream_trace_id");
    expect(container.textContent).toContain("11111111111111111111111111111111");
    expect(container.textContent).toContain("latest_trace_id");
    expect(container.textContent).toContain("22222222222222222222222222222222");
    expect(onOpenLogs).toHaveBeenCalledTimes(1);
  });
});

async function renderBlock(onOpenLogs = vi.fn()): Promise<void> {
  await act(async () => {
    root.render(<ConversationTraceBlock onOpenLogs={onOpenLogs} />);
  });
}
