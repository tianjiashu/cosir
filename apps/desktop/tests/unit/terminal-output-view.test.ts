import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type MockTerminalInstance = {
  options: Record<string, unknown>;
  cols: number;
  rows: number;
  buffer: { active: { baseY: number; cursorY: number; cursorX: number } };
  resize: ReturnType<typeof vi.fn>;
  open: ReturnType<typeof vi.fn>;
  loadAddon: ReturnType<typeof vi.fn>;
  write: ReturnType<typeof vi.fn>;
  reset: ReturnType<typeof vi.fn>;
  dispose: ReturnType<typeof vi.fn>;
};

const mocks = vi.hoisted(() => ({
  terminals: [] as MockTerminalInstance[],
  fitAddons: [] as Array<{ proposeDimensions: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn> }>,
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class MockTerminal {
    options: Record<string, unknown>;
    cols = 80;
    rows = 24;
    buffer = { active: { baseY: 0, cursorY: 0, cursorX: 0 } };
    unicode = { activeVersion: "6" };
    resize = vi.fn((cols: number, rows: number) => {
      this.cols = cols;
      this.rows = rows;
    });
    open = vi.fn();
    loadAddon = vi.fn();
    write = vi.fn((_text: string, done?: () => void) => done?.());
    reset = vi.fn();
    dispose = vi.fn();

    constructor(options: Record<string, unknown>) {
      this.options = options;
      mocks.terminals.push(this);
    }
  },
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class MockFitAddon {
    proposeDimensions = vi.fn((): { cols: number; rows: number } | undefined => undefined);
    dispose = vi.fn();

    constructor() {
      mocks.fitAddons.push(this);
    }
  },
}));

vi.mock("@xterm/addon-unicode11", () => ({
  Unicode11Addon: class MockUnicode11Addon {},
}));

// 配色从 CSS token 读取，依赖真实 DOM；这里只验证快照回放逻辑。
vi.mock("@/components/terminal/xterm-appearance", () => ({
  xtermAppearance: () => ({}),
}));

import { TerminalOutputView } from "@/components/assistant-ui/tools/terminal-output-view";

describe("TerminalOutputView", () => {
  beforeEach(() => {
    mocks.terminals.length = 0;
    mocks.fitAddons.length = 0;
    // 尺寸控制器的 animation frame 不执行：这里只验证快照回放，不验证布局 fit。
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.stubGlobal("window", { requestAnimationFrame: () => 1, cancelAnimationFrame: () => undefined });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("replays the cumulative snapshot into every new xterm instance", () => {
    const first = new TerminalOutputView({} as HTMLElement);
    first.applySnapshot({ output: "first\n" });
    first.dispose();

    const second = new TerminalOutputView({} as HTMLElement);
    second.applySnapshot({ output: "first\n" });

    expect(mocks.terminals).toHaveLength(2);
    expect(mocks.terminals[0].write).toHaveBeenCalledWith("first\n", expect.any(Function));
    expect(mocks.terminals[1].write).toHaveBeenCalledWith("first\n", expect.any(Function));
  });

  it("converts LF-only pipe output into terminal newlines", () => {
    const session = new TerminalOutputView({} as HTMLElement);
    void session;

    expect(mocks.terminals[0].options.convertEol).toBe(true);
  });

  it("reuses the reconciler for append and reset operations", () => {
    const session = new TerminalOutputView({} as HTMLElement);
    session.applySnapshot({ output: "progress 1" });
    session.applySnapshot({ output: "progress 1\rprogress 2" });
    session.applySnapshot({ output: "final screen" });

    const terminal = mocks.terminals[0];
    expect(terminal.write).toHaveBeenNthCalledWith(1, "progress 1", expect.any(Function));
    expect(terminal.write).toHaveBeenNthCalledWith(2, "\rprogress 2", expect.any(Function));
    expect(terminal.reset).toHaveBeenCalledOnce();
    expect(terminal.write).toHaveBeenNthCalledWith(3, "final screen", expect.any(Function));
  });

  it("coalesces snapshots while xterm is still parsing a write", () => {
    let releaseWrite: (() => void) | undefined;
    const session = new TerminalOutputView({} as HTMLElement);
    const xterm = mocks.terminals[0];
    xterm.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "one" });
    session.applySnapshot({ output: "one\ntwo" });
    session.applySnapshot({ output: "one\ntwo\nthree" });

    expect(xterm.write).toHaveBeenCalledTimes(1);
    releaseWrite?.();

    expect(xterm.write).toHaveBeenCalledTimes(2);
    expect(xterm.write).toHaveBeenLastCalledWith("\ntwo\nthree", expect.any(Function));
  });

  it("keeps control sequences intact while coalescing cumulative snapshots", () => {
    let releaseWrite: (() => void) | undefined;
    const session = new TerminalOutputView({} as HTMLElement);
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "\u001b[" });
    session.applySnapshot({ output: "\u001b[2Kready" });
    releaseWrite?.();

    expect(terminal.write).toHaveBeenLastCalledWith("2Kready", expect.any(Function));
  });

  it("recovers after a synchronous xterm write failure", () => {
    const session = new TerminalOutputView({} as HTMLElement);
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce(() => {
      throw new Error("write failed");
    });

    expect(() => session.applySnapshot({ output: "failed" })).not.toThrow();
    session.applySnapshot({ output: "recovered" });

    expect(terminal.write).toHaveBeenCalledTimes(2);
  });

  it("does not schedule layout work after disposal", () => {
    let releaseWrite: (() => void) | undefined;
    const session = new TerminalOutputView({} as HTMLElement);
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "pending" });
    session.dispose();
    releaseWrite?.();

    expect(terminal.dispose).toHaveBeenCalledOnce();
  });

  it("sizes rows to the written content and caps them at the visible maximum", () => {
    const session = new TerminalOutputView({} as HTMLElement);
    const terminal = mocks.terminals[0];
    mocks.fitAddons[0].proposeDimensions.mockReturnValue({ cols: 100, rows: 3 });

    // 两行输出、以换行结尾：光标停在第 3 行行首，光标行也要计入，否则首行会被挤出视口。
    terminal.buffer.active = { baseY: 0, cursorY: 2, cursorX: 0 };
    session.applySnapshot({ output: "a\nb\n" });
    expect(terminal.resize).toHaveBeenLastCalledWith(100, 3);

    terminal.buffer.active = { baseY: 40, cursorY: 1, cursorX: 3 };
    session.applySnapshot({ output: "a\nb\nmore" });
    expect(terminal.resize).toHaveBeenLastCalledWith(100, 16);
  });

  it("disposes xterm resources", () => {
    const session = new TerminalOutputView({} as HTMLElement);
    session.dispose();

    expect(mocks.fitAddons[0].dispose).toHaveBeenCalledOnce();
    expect(mocks.terminals[0].dispose).toHaveBeenCalledOnce();
  });
});
