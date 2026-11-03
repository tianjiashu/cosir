import { Terminal } from "@xterm/xterm";
import { describe, expect, it } from "vitest";

function screen(terminal: Terminal): string {
  const buffer = terminal.buffer.active;
  return Array.from({ length: buffer.length }, (_, row) => buffer.getLine(row)?.translateToString(true) ?? "")
    .join("\n")
    .trim();
}

/**
 * 守住 `TerminalView.reset` 依赖的 xterm 行为：`reset()` 不清写缓冲，把它放进 `write("", cb)` 的回调里，
 * 就排在已排队输出之后、之后写入的输出之前执行。xterm 升级若改了写队列语义，这里先失败。
 */
describe("xterm 写队列与 reset 的顺序", () => {
  it("排进写队列的 reset 只清掉之前的输出，不影响之后写入的重放", async () => {
    const terminal = new Terminal({ cols: 40, rows: 5, allowProposedApi: true });
    terminal.write("stale output");
    terminal.write("", () => terminal.reset());
    await new Promise<void>((resolve) => terminal.write("replayed", resolve));
    expect(screen(terminal)).toBe("replayed");
    terminal.dispose();
  });

  it("直接调用 reset 时，还没解析的旧输出会画在清屏之后（这正是要避开的竞态）", async () => {
    const terminal = new Terminal({ cols: 40, rows: 5, allowProposedApi: true });
    terminal.write("stale output");
    terminal.reset();
    await new Promise<void>((resolve) => terminal.write(" replayed", resolve));
    expect(screen(terminal)).toBe("stale output replayed");
    terminal.dispose();
  });
});
