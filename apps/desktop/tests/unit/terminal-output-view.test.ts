import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type MockTerminalInstance = {
  options: Record<string, unknown>;
  cols: number;
  rows: number;
  buffer: { active: { baseY: number; cursorY: number; cursorX: number; length?: number; viewportY?: number; getLine?: ReturnType<typeof vi.fn> }; normal: { length: number } };
  resize: ReturnType<typeof vi.fn>;
  open: ReturnType<typeof vi.fn>;
  loadAddon: ReturnType<typeof vi.fn>;
  write: ReturnType<typeof vi.fn>;
  reset: ReturnType<typeof vi.fn>;
  dispose: ReturnType<typeof vi.fn>;
  scrollToBottom: ReturnType<typeof vi.fn>;
  scrollToLine: ReturnType<typeof vi.fn>;
  selectAll: ReturnType<typeof vi.fn>;
  getSelection: ReturnType<typeof vi.fn>;
  clearSelection: ReturnType<typeof vi.fn>;
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
    buffer = { active: { baseY: 0, cursorY: 0, cursorX: 0, length: 1, getLine: vi.fn(() => undefined) }, normal: { length: 0 } };
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
    scrollToBottom = vi.fn();
    scrollToLine = vi.fn();
    attachCustomWheelEventHandler = vi.fn();
    onScroll = vi.fn(() => ({ dispose: vi.fn() }));
    selectAll = vi.fn();
    getSelection = vi.fn(() => "");
    clearSelection = vi.fn();

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
  xtermPaneFont: () => ({}),
  xtermCardTheme: () => ({}),
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
    const first = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    first.applySnapshot({ output: "first\n" });
    first.dispose();

    const second = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    second.applySnapshot({ output: "first\n" });

    expect(mocks.terminals).toHaveLength(2);
    expect(mocks.terminals[0].write).toHaveBeenCalledWith("first\n", expect.any(Function));
    expect(mocks.terminals[1].write).toHaveBeenCalledWith("first\n", expect.any(Function));
  });

  it("converts LF-only pipe output into terminal newlines", () => {
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    void session;

    expect(mocks.terminals[0].options.convertEol).toBe(true);
  });

  it("reuses the reconciler for append and reset operations", () => {
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
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
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
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
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
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
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
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
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "pending" });
    session.dispose();
    releaseWrite?.();

    expect(terminal.dispose).toHaveBeenCalledOnce();
  });

  // 目的：只在回滚区开始丢最早的输出时提示；潜在缺陷：清屏序列被误当成丢行，或清屏重放后提示不复位。
  it("reports truncation when the buffer reaches capacity, and clears it after a smaller replay", () => {
    const changes: boolean[] = [];
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light", onTruncatedChange: (value) => changes.push(value) });
    const terminal = mocks.terminals[0];
    mocks.fitAddons[0].proposeDimensions.mockReturnValue({ cols: 100, rows: 3 });

    terminal.buffer.normal.length = 100;
    session.applySnapshot({ output: "short" });
    expect(changes).toEqual([]);

    terminal.buffer.active = { baseY: 6000, cursorY: 15, cursorX: 0, length: 6016 };
    terminal.buffer.normal.length = 16 + 5000;
    session.applySnapshot({ output: "short + a lot more" });
    expect(changes).toEqual([true]);

    terminal.buffer.active = { baseY: 0, cursorY: 1, cursorX: 0, length: 2 };
    terminal.buffer.normal.length = 2;
    session.applySnapshot({ output: "new screen" });
    expect(terminal.reset).toHaveBeenCalledOnce();
    expect(changes).toEqual([true, false]);
  });

  it("copies public buffer lines without changing the user selection", () => {
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    const terminal = mocks.terminals[0];
    const lines = [
      { isWrapped: false, translateToString: vi.fn(() => "long ") },
      { isWrapped: true, translateToString: vi.fn(() => "中文") },
      { isWrapped: false, translateToString: vi.fn(() => "tail") },
    ];
    terminal.buffer.active = { baseY: 0, cursorY: 0, cursorX: 4, length: 3, getLine: vi.fn((i) => lines[i]) };
    expect(session.copyText()).toBe("long 中文\ntail");
    expect(terminal.selectAll).not.toHaveBeenCalled();
    expect(terminal.clearSelection).not.toHaveBeenCalled();
    expect(lines[0].translateToString).toHaveBeenCalledWith(false);
  });

  it("restores the reader before releasing a queued write", () => {
    const view = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    const terminal = mocks.terminals[0];
    terminal.buffer.active.viewportY = 0;
    view.applySnapshot({ output: "start" });
    terminal.buffer.active.baseY = 10;
    terminal.buffer.active.viewportY = 0;
    terminal.scrollToLine.mockImplementation((line) => { terminal.buffer.active.viewportY = line; });
    let release: (() => void) | undefined;
    terminal.write.mockImplementationOnce((_text, done) => { release = done; });
    view.applySnapshot({ output: "start one" });
    view.applySnapshot({ output: "start one two" });
    terminal.buffer.active.viewportY = 10; // xterm parse briefly exposes its new scroll position.
    release?.();
    expect(terminal.scrollToLine).toHaveBeenLastCalledWith(0);
    expect(terminal.scrollToBottom).toHaveBeenCalledTimes(1);
  });

  it("ignores a write callback after the viewport was disposed", () => {
    const view = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    const terminal = mocks.terminals[0];
    let release: (() => void) | undefined;
    terminal.write.mockImplementationOnce((_text, done) => { release = done; });
    view.applySnapshot({ output: "pending" });
    view.dispose();
    release?.();
    expect(terminal.scrollToBottom).not.toHaveBeenCalled();
  });

  it("disposes xterm resources", () => {
    const session = new TerminalOutputView({ addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as HTMLElement, { resolvedTheme: "light" });
    session.dispose();

    expect(mocks.fitAddons[0].dispose).toHaveBeenCalledOnce();
    expect(mocks.terminals[0].dispose).toHaveBeenCalledOnce();
  });
});
