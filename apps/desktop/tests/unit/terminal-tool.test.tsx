import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { TerminalTool } from "@/components/assistant-ui/tools/terminal-tool";

type RenderOptions = {
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  args?: Record<string, string>;
  display_data?: Record<string, unknown> | null;
  taskId?: number;
  taskCancelling?: boolean;
};

function renderTerminal({ status, args = {}, display_data = null, taskId, taskCancelling = false }: RenderOptions): string {
  return renderToStaticMarkup(
    <TerminalTool
      type="tool-call"
      toolName="execute_terminal"
      toolCallId="call-1"
      args={args}
      argsText={JSON.stringify(args)}
      status={{ type: status === "completed" ? "complete" : "running" } as never}
      addResult={() => undefined}
      resume={() => undefined}
      respondToApproval={() => undefined}
      taskId={taskId}
      taskCancelling={taskCancelling}
      artifact={{
        backendStatus: status,
        presentation: { verb: "运行命令", surface: "standalone", expandable: true, expand_layout: "terminal" },
        display_data,
      }}
    />,
  );
}

describe("TerminalTool", () => {
  it("renders command and shell from tool args before display data exists", () => {
    const html = renderTerminal({ status: "running", args: { command: "pnpm test", shell: "pwsh" } });

    expect(html).toContain("pnpm test");
    expect(html).toContain("pwsh");
    expect(html).toContain("执行中");
    expect(html).not.toContain("exit ");
  });

  it("falls back to the tool name when neither args nor display data carry a command", () => {
    const html = renderTerminal({ status: "running" });

    expect(html).toContain("execute_terminal");
  });

  it("renders a success exit code pill after display data arrives", () => {
    const html = renderTerminal({
      status: "completed",
      args: { command: "pnpm test" },
      display_data: { kind: "terminal-result", command: "pnpm test", output: "ok", exit_code: 0 },
    });

    expect(html).toContain("exit 0");
    expect(html).toContain("已完成");
  });

  it("renders a non-zero exit code pill without claiming success", () => {
    const html = renderTerminal({
      status: "completed",
      args: { command: "pnpm test" },
      display_data: { kind: "terminal-result", command: "pnpm test", output: "", exit_code: 2 },
    });

    expect(html).toContain("exit 2");
    expect(html).not.toContain("exit 0");
  });

  it("renders live terminal output and reports the streaming display budget", () => {
    const html = renderTerminal({
      status: "running",
      args: { command: "pnpm test" },
      display_data: {
        kind: "terminal-result",
        output: "first chunk",
        stream_truncated: true,
      },
    });

    expect(html).toContain('data-terminal-viewport="true"');
    expect(html).toContain("实时输出已达到展示上限");
    expect(html).not.toContain("exit ");
  });

  it("shows a separate stop action only for a running terminal call with its task identity", () => {
    const running = renderTerminal({ status: "running", taskId: 24 });
    const pending = renderTerminal({ status: "pending", taskId: 24 });
    const missingTask = renderTerminal({ status: "running" });
    const taskStopping = renderTerminal({ status: "running", taskId: 24, taskCancelling: true });

    expect(running).toContain('aria-label="停止终端命令"');
    expect(running).toContain("停止</span>");
    expect(pending).not.toContain('aria-label="停止终端命令"');
    expect(missingTask).not.toContain('aria-label="停止终端命令"');
    expect(taskStopping).not.toContain('aria-label="停止终端命令"');
  });

  it("hides the stop action after the tool reaches a terminal status", () => {
    for (const status of ["completed", "failed", "cancelled"] as const) {
      const html = renderTerminal({ status, taskId: 24 });
      expect(html).not.toContain('aria-label="停止终端命令"');
    }
  });
});
