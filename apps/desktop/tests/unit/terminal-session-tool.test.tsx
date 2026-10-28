import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { terminalSession } from "./transport-fixtures";

// 服务端渲染只读 zustand 的初始状态，这里直接替换 store，让卡片读到受控的 snapshot 会话。
const store = vi.hoisted(() => ({
  sessions: [] as TerminalSessionEntry[],
  reveal: vi.fn(),
}));
vi.mock("@/lib/terminal/terminal-workbench-store", () => {
  const state = () => ({ tasks: { 42: { sessions: store.sessions } } });
  const useTerminalWorkbenchStore = Object.assign(
    (selector: (value: ReturnType<typeof state>) => unknown) => selector(state()),
    { getState: () => ({ reveal: store.reveal }) },
  );
  return { useTerminalWorkbenchStore };
});

import { TerminalSessionTool } from "@/components/assistant-ui/tools/terminal-session-tool";

type RenderOptions = {
  variant: string;
  status?: "pending" | "running" | "completed" | "failed" | "cancelled";
  display_data?: Record<string, unknown> | null;
  interactive?: boolean;
};

function renderSession({ variant, status = "completed", display_data = {}, interactive = true }: RenderOptions): string {
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
      taskId={42}
      interactive={interactive}
      artifact={{
        backendStatus: status,
        presentation: { variant, surface: variant.endsWith("start") ? "standalone" : "trace" },
        display_data: display_data === null ? null : { kind: "terminal-session", ...display_data },
      }}
    />,
  );
}

describe("TerminalSessionTool", () => {
  beforeEach(() => {
    store.sessions = [terminalSession("term_demo", { createdBy: "agent", shellKind: "powershell" })];
  });

  it("renders terminal_list as a discovery result", () => {
    const html = renderSession({ variant: "terminal-session-list", display_data: { session_count: 2 } });
    expect(html).toContain("列出终端会话");
    expect(html).toContain("2 个会话");
  });

  it("start 卡提供「在终端中查看」，状态读 snapshot 的实时会话", () => {
    store.sessions = [terminalSession("term_demo", { status: "exited", exitCode: 0 })];
    const html = renderSession({
      variant: "terminal-session-start",
      display_data: { session_id: "term_demo", shell_kind: "powershell", initial_cwd: "H:\\coding-agent", status: "running" },
    });
    expect(html).toContain("在终端中查看");
    expect(html).toContain("powershell");
    expect(html).toContain("已退出");
    expect(html).not.toContain("运行中");
  });

  it("会话已不在 snapshot（后端重启或超出保留数）时标为已失效且不可跳转", () => {
    store.sessions = [];
    const html = renderSession({ variant: "terminal-session-start", display_data: { session_id: "term_demo" } });
    expect(html).toContain("已失效");
    expect(html).toMatch(/<button[^>]*disabled/);
  });

  it("只读线程（子 Agent）不提供跳转", () => {
    const start = renderSession({ variant: "terminal-session-start", display_data: { session_id: "term_demo" }, interactive: false });
    expect(start).not.toContain("在终端中查看");
    const write = renderSession({ variant: "terminal-session-write", display_data: { session_id: "term_demo", input_summary: "npm test" }, interactive: false });
    expect(write).toContain('role="status"');
  });

  it("write 动作行显示实际输入与等待结果，可点击跳转", () => {
    const html = renderSession({
      variant: "terminal-session-write",
      display_data: { session_id: "term_demo", input_type: "line", input_summary: "npm test", wait_result: { until: "command_returned", end_reason: "command_returned" }, lines: [12, 40] },
    });
    expect(html).toContain("终端输入");
    expect(html).toContain("npm test · 命令已返回");
    expect(html).toContain('role="button"');
    expect(html).not.toContain("next seq");
  });

  it("read 动作行显示行范围；无等待条件时不显示等待结果", () => {
    const html = renderSession({
      variant: "terminal-session-read",
      display_data: { session_id: "term_demo", lines: [120, 182], wait_result: { until: "output_quiet", end_reason: "idle" } },
    });
    expect(html).toContain("读取终端");
    expect(html).toContain("第 120–182 行");
    expect(html).not.toContain("idle");
  });

  it("全屏程序期间 read 显示「全屏画面」而不是行号", () => {
    const html = renderSession({
      variant: "terminal-session-read",
      display_data: { session_id: "term_demo", lines: null, screen: true },
    });
    expect(html).toContain("全屏画面");
    expect(html).not.toContain("第 ");
  });

  it("renders terminal_close as a final trace row", () => {
    const html = renderSession({
      variant: "terminal-session-close",
      display_data: { session_id: "term_demo", status: "closed", exit_code: 0 },
    });
    expect(html).toContain("关闭终端会话");
    expect(html).toContain("已关闭 · exit 0");
    expect(html).toContain('role="status"');
  });

  it("signal 变体已并入 write，按未知变体安全降级", () => {
    const html = renderSession({ variant: "terminal-session-signal", display_data: { session_id: "term_demo" } });
    expect(html).toContain("终端会话操作");
  });
});
