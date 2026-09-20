import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { TerminalSessionTool } from "@/components/assistant-ui/tools/terminal-session-tool";

type RenderOptions = {
  variant: string;
  status?: "pending" | "running" | "completed" | "failed" | "cancelled";
  display_data?: Record<string, unknown> | null;
  taskId?: number;
};

function renderSession({ variant, status = "completed", display_data = {}, taskId = 42 }: RenderOptions): string {
  return renderToStaticMarkup(
    <TerminalSessionTool
      type="tool-call"
      toolName="terminal_start"
      toolCallId="call-session-1"
      args={{}}
      argsText="{}"
      status={{ type: status === "completed" ? "complete" : "running" } as never}
      addResult={() => undefined}
      resume={() => undefined}
      respondToApproval={() => undefined}
      taskId={taskId}
      artifact={{
        backendStatus: status,
        presentation: { variant, surface: variant.endsWith("start") ? "standalone" : "trace" },
        display_data: display_data === null ? null : { kind: "terminal-session", ...display_data },
      }}
    />,
  );
}

describe("TerminalSessionTool", () => {
  it("renders terminal_start as the session anchor with the only open action", () => {
    const html = renderSession({
      variant: "terminal-session-start",
      display_data: {
        session_id: "term_demo",
        shell_kind: "powershell",
        initial_cwd: "H:\\coding-agent",
        status: "running",
      },
    });

    expect(html).toContain("终端会话");
    expect(html).toContain("powershell");
    expect(html).toContain("打开终端");
  });

  it("renders terminal_read as a compact trace without a panel action", () => {
    const html = renderSession({
      variant: "terminal-session-read",
      display_data: { session_id: "term_demo", next_seq: 18, status: "running" },
    });

    expect(html).toContain("读取终端输出");
    expect(html).toContain("next seq 18");
    expect(html).not.toContain("打开终端");
  });

  it("shows only the safe submit state for terminal_write", () => {
    const html = renderSession({
      variant: "terminal-session-write",
      display_data: { session_id: "term_demo", submitted: true, status: "running" },
    });

    expect(html).toContain("提交终端输入");
    expect(html).toContain("已提交");
    expect(html).not.toContain("打开终端");
    expect(html).not.toContain("password");
  });

  it("renders the selected signal without exposing a terminal control", () => {
    const html = renderSession({
      variant: "terminal-session-signal",
      display_data: { session_id: "term_demo", signal: "interrupt", status: "running" },
    });

    expect(html).toContain("发送中断信号");
    expect(html).not.toContain("打开终端");
  });

  it("renders terminal_close as a final trace row", () => {
    const html = renderSession({
      variant: "terminal-session-close",
      display_data: { session_id: "term_demo", status: "closed", exit_code: 0 },
    });

    expect(html).toContain("关闭终端会话");
    expect(html).toContain("已关闭");
    expect(html).toContain("exit 0");
    expect(html).not.toContain("打开终端");
  });
});
