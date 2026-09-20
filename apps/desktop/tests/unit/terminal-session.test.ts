import { beforeEach, describe, expect, it, vi } from "vitest";

type MockTerminalInstance = {
  options: Record<string, unknown>;
  open: ReturnType<typeof vi.fn>;
  loadAddon: ReturnType<typeof vi.fn>;
  write: ReturnType<typeof vi.fn>;
  reset: ReturnType<typeof vi.fn>;
  dispose: ReturnType<typeof vi.fn>;
};

const mocks = vi.hoisted(() => ({
  terminals: [] as MockTerminalInstance[],
  fitAddons: [] as Array<{ fit: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn> }>,
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class MockTerminal {
    options: Record<string, unknown>;
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
    fit = vi.fn();
    dispose = vi.fn();

    constructor() {
      mocks.fitAddons.push(this);
    }
  },
}));

import { TerminalSession } from "@/components/assistant-ui/tools/terminal-session";

describe("TerminalSession", () => {
  beforeEach(() => {
    mocks.terminals.length = 0;
    mocks.fitAddons.length = 0;
  });

  it("replays the cumulative snapshot into every new xterm instance", () => {
    const first = new TerminalSession({} as HTMLElement);
    first.applySnapshot({ output: "first\n", outputSeq: 1 });
    first.dispose();

    const second = new TerminalSession({} as HTMLElement);
    second.applySnapshot({ output: "first\n", outputSeq: 1 });

    expect(mocks.terminals).toHaveLength(2);
    expect(mocks.terminals[0].write).toHaveBeenCalledWith("first\n", expect.any(Function));
    expect(mocks.terminals[1].write).toHaveBeenCalledWith("first\n", expect.any(Function));
  });

  it("converts LF-only pipe output into terminal newlines", () => {
    const session = new TerminalSession({} as HTMLElement);
    void session;

    expect(mocks.terminals[0].options.convertEol).toBe(true);
  });

  it("reuses the reconciler for append and reset operations", () => {
    const session = new TerminalSession({} as HTMLElement);
    session.applySnapshot({ output: "progress 1", outputSeq: 1 });
    session.applySnapshot({ output: "progress 1\rprogress 2", outputSeq: 2 });
    session.applySnapshot({ output: "final screen", outputSeq: 3 });

    const terminal = mocks.terminals[0];
    expect(terminal.write).toHaveBeenNthCalledWith(1, "progress 1", expect.any(Function));
    expect(terminal.write).toHaveBeenNthCalledWith(2, "\rprogress 2", expect.any(Function));
    expect(terminal.reset).toHaveBeenCalledOnce();
    expect(terminal.write).toHaveBeenNthCalledWith(3, "final screen", expect.any(Function));
  });

  it("coalesces snapshots while xterm is still parsing a write", () => {
    let releaseWrite: (() => void) | undefined;
    const session = new TerminalSession({} as HTMLElement);
    const xterm = mocks.terminals[0];
    xterm.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "one", outputSeq: 1 });
    session.applySnapshot({ output: "one\ntwo", outputSeq: 2 });
    session.applySnapshot({ output: "one\ntwo\nthree", outputSeq: 3 });

    expect(xterm.write).toHaveBeenCalledTimes(1);
    releaseWrite?.();

    expect(xterm.write).toHaveBeenCalledTimes(2);
    expect(xterm.write).toHaveBeenLastCalledWith("\ntwo\nthree", expect.any(Function));
  });

  it("keeps control sequences intact while coalescing cumulative snapshots", () => {
    let releaseWrite: (() => void) | undefined;
    const session = new TerminalSession({} as HTMLElement);
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "\u001b[", outputSeq: 1 });
    session.applySnapshot({ output: "\u001b[2Kready", outputSeq: 2 });
    releaseWrite?.();

    expect(terminal.write).toHaveBeenLastCalledWith("2Kready", expect.any(Function));
  });

  it("ignores an older snapshot while a write is pending", () => {
    let releaseWrite: (() => void) | undefined;
    const session = new TerminalSession({} as HTMLElement);
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "latest", outputSeq: 3 });
    session.applySnapshot({ output: "older", outputSeq: 2 });
    releaseWrite?.();

    expect(terminal.write).toHaveBeenCalledTimes(1);
    expect(terminal.reset).not.toHaveBeenCalled();
  });

  it("recovers after a synchronous xterm write failure", () => {
    const session = new TerminalSession({} as HTMLElement);
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce(() => {
      throw new Error("write failed");
    });

    expect(() => session.applySnapshot({ output: "failed", outputSeq: 1 })).not.toThrow();
    session.applySnapshot({ output: "recovered", outputSeq: 2 });

    expect(terminal.write).toHaveBeenCalledTimes(2);
  });

  it("does not schedule layout work after disposal", () => {
    let releaseWrite: (() => void) | undefined;
    const session = new TerminalSession({} as HTMLElement);
    const terminal = mocks.terminals[0];
    terminal.write.mockImplementationOnce((_text: string, done?: () => void) => {
      releaseWrite = done;
    });

    session.applySnapshot({ output: "pending", outputSeq: 1 });
    session.dispose();
    releaseWrite?.();

    expect(terminal.dispose).toHaveBeenCalledOnce();
  });

  it("disposes xterm resources and schedules an initial fit", () => {
    const session = new TerminalSession({} as HTMLElement);
    session.dispose();

    expect(mocks.fitAddons[0].fit).toHaveBeenCalledOnce();
    expect(mocks.fitAddons[0].dispose).toHaveBeenCalledOnce();
    expect(mocks.terminals[0].dispose).toHaveBeenCalledOnce();
  });
});
