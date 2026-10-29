import { describe, expect, it } from "vitest";

import { terminalPaneStatus } from "@/lib/terminal/terminal-pane-status";

import { terminalSession } from "./transport-fixtures";

describe("终端面板常驻状态", () => {
  it("没有选中会话、首次连接或已连接时不出状态栏（避免终端行数抖动）", () => {
    expect(terminalPaneStatus(null, "failed")).toBeNull();
    expect(terminalPaneStatus(terminalSession("t1"), undefined)).toBeNull();
    expect(terminalPaneStatus(terminalSession("t1"), "connecting")).toBeNull();
    expect(terminalPaneStatus(terminalSession("t1"), "connected")).toBeNull();
  });

  it("断线与失败就地给出重连操作", () => {
    expect(terminalPaneStatus(terminalSession("t1"), "reconnecting")).toMatchObject({ tone: "warning", action: "reconnect" });
    expect(terminalPaneStatus(terminalSession("t1"), "failed")).toMatchObject({ tone: "destructive", action: "reconnect" });
  });

  it("会话失效是平静提示，并就地给出新建终端", () => {
    expect(terminalPaneStatus(terminalSession("t1"), "expired")).toMatchObject({ tone: "muted", action: "create" });
  });

  it("会话结束带退出码，已结束会话提示画面可能不完整", () => {
    const status = terminalPaneStatus(terminalSession("t1", { status: "exited", exitCode: 2 }), "ended");
    expect(status).toMatchObject({ tone: "muted", action: null });
    expect(status?.text).toBe("会话已结束，输入已停用 · 退出码 2 · 画面受缓冲限制可能不完整");
  });
});
