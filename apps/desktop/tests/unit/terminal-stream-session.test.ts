import { describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  resize: vi.fn(), write: vi.fn(), reset: vi.fn(), clear: vi.fn(), dispose: vi.fn(),
}));
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    open() {}
    resize = state.resize;
    write = state.write;
    reset = state.reset;
    clear = state.clear;
    dispose = state.dispose;
  },
}));
vi.mock("@/components/terminal/xterm-appearance", () => ({ xtermAppearance: () => ({}) }));
import { TerminalStreamSession } from "@/components/terminal/terminal-stream-session";

describe("worker-owned preview geometry", () => {
  it("uses attached geometry before writing raw bytes, without fitting the container", () => {
    const preview = new TerminalStreamSession({} as HTMLElement);
    preview.setGeometry(24, 80);
    const bytes = new Uint8Array([65, 13, 66]);
    preview.write(bytes);
    expect(state.resize).toHaveBeenCalledExactlyOnceWith(80, 24);
    expect(state.write).toHaveBeenCalledExactlyOnceWith(bytes);
    expect(state.resize.mock.invocationCallOrder[0]).toBeLessThan(state.write.mock.invocationCallOrder[0]);
    preview.clear();
    expect(state.reset).toHaveBeenCalledOnce();
    preview.dispose();
    expect(state.dispose).toHaveBeenCalledOnce();
  });
});
