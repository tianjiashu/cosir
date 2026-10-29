import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { TerminalTool } from "@/components/assistant-ui/tools/terminal-tool";

type RenderOptions = {
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  args?: Record<string, string>;
  display_data?: Record<string, unknown> | null;
  taskCancelling?: boolean;
  deferred?: boolean;
  interactive?: boolean;
};

function renderTerminal({ status, args = {}, display_data = null, taskCancelling = false, deferred = false, interactive }: RenderOptions): string {
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
      taskId={24}
      taskCancelling={taskCancelling}
      interactive={interactive}
      artifact={{
        deferred,
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

  it("does not repeat a zero exit code next to the success status", () => {
    const html = renderTerminal({
      status: "completed",
      args: { command: "pnpm test" },
      display_data: { kind: "terminal-result", command: "pnpm test", output: "ok", exit_code: 0 },
    });

    expect(html).not.toContain("exit 0");
    expect(html).toContain("已完成");
    expect(html).not.toContain("data-terminal-failed");
  });

  it("renders a non-zero exit code pill without claiming success", () => {
    const html = renderTerminal({
      status: "completed",
      args: { command: "pnpm test" },
      display_data: { kind: "terminal-result", command: "pnpm test", output: "", exit_code: 2 },
    });

    expect(html).toContain("exit 2");
    expect(html).not.toContain("exit 0");
    expect(html).not.toContain("已完成");
    expect(html).toContain('data-terminal-failed="true"');
  });

  it("marks a deferred placeholder in the header instead of a pending status", () => {
    const html = renderTerminal({
      status: "pending",
      args: { command: "pnpm dev" },
      display_data: { kind: "terminal-result", command: "pnpm dev" },
      deferred: true,
    });

    expect(html).toContain("后台");
    expect(html).toContain("后台运行中");
    expect(html).not.toContain("准备执行");
    expect(html).toContain('aria-label="停止终端命令"');
  });

  it("renders background output like a foreground command once it streams in", () => {
    const running = renderTerminal({
      status: "pending",
      args: { command: "make watch" },
      display_data: { kind: "terminal-result", command: "make watch", output: "step 1" },
      deferred: true,
    });
    expect(running).toContain('data-terminal-viewport="true"');
    expect(running).toContain("后台运行中");

    const ended = renderTerminal({
      status: "completed",
      args: { command: "make" },
      display_data: { kind: "terminal-result", command: "make", output: "", exit_code: 3 },
      deferred: true,
    });
    // 结束后与前台一样收起，表头仍给出结论。
    expect(ended).toContain("exit 3");
  });

  it("leaves the stop action to a read-only surface's own controls", () => {
    const html = renderTerminal({ status: "pending", deferred: true, interactive: false });

    expect(html).toContain("后台运行中");
    expect(html).not.toContain('aria-label="停止终端命令"');
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

  it("shows a separate stop action only for a running terminal call", () => {
    const running = renderTerminal({ status: "running" });
    const pending = renderTerminal({ status: "pending" });
    const taskStopping = renderTerminal({ status: "running", taskCancelling: true });

    expect(running).toContain('aria-label="停止终端命令"');
    expect(pending).not.toContain('aria-label="停止终端命令"');
    expect(taskStopping).not.toContain('aria-label="停止终端命令"');
  });

  it("hides the stop action after the tool reaches a terminal status", () => {
    for (const status of ["completed", "failed", "cancelled"] as const) {
      const html = renderTerminal({ status });
      expect(html).not.toContain('aria-label="停止终端命令"');
    }
  });
});
