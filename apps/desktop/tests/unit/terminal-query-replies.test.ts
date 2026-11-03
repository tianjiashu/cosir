import { Terminal } from "@xterm/xterm";
import { describe, expect, it } from "vitest";

import { suppressTerminalQueryReplies } from "@/components/terminal/terminal-query-replies";

function write(terminal: Terminal, data: string): Promise<void> {
  return new Promise((resolve) => terminal.write(data, resolve));
}

describe("terminal query replies", () => {
  // 目的：终端查询只由 Worker 应答；潜在缺陷：xterm 也应答，程序收到两份，多余的一份成了「用户输入」。
  it("does not answer device, status, mode or setting queries", async () => {
    const terminal = new Terminal({ allowProposedApi: true });
    suppressTerminalQueryReplies(terminal);
    const replies: string[] = [];
    terminal.onData((data) => replies.push(data));

    await write(terminal, "\x1b[c\x1b[>c\x1b[5n\x1b[6n\x1b[?6n\x1b[?25$p\x1b[4$p\x1bP$qm\x1b\\");

    expect(replies).toEqual([]);
    terminal.dispose();
  });

  it("still lets xterm answer when not suppressed (guards the test itself)", async () => {
    const terminal = new Terminal({ allowProposedApi: true });
    const replies: string[] = [];
    terminal.onData((data) => replies.push(data));

    await write(terminal, "\x1b[6n");

    expect(replies.join("")).toMatch(/\x1b\[\d+;\d+R/);
    terminal.dispose();
  });

  it("keeps ordinary output and other CSI sequences working", async () => {
    const terminal = new Terminal({ allowProposedApi: true });
    suppressTerminalQueryReplies(terminal);

    await write(terminal, "ab\x1b[2Dc\x1b[31mx");

    expect(terminal.buffer.active.getLine(0)?.translateToString(true)).toBe("cx");
    terminal.dispose();
  });
});
