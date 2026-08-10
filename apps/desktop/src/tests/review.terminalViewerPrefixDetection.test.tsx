// @vitest-environment happy-dom
/**
 * 缺陷验证 #8：TerminalViewer 前缀检测含恒真死代码，同长度内容替换检测不到。
 *
 * 背景：TerminalViewer 约 120 行的「回退检测」：
 *   if (output.length < written || !output.startsWith(output.slice(0, written)))
 * 其中 `output.startsWith(output.slice(0, written))` 恒为 true（字符串必以自身前缀
 * 开头），取反后恒 false——该析取支是死代码。后果：同长度（或更长但前缀已变）的
 * output 整体替换（如切换到另一条命令的同等长度输出）检测不到，既不清屏也不重写，
 * 旧内容残留，新内容仅从 written 处切片（空串）→ 视图与 props 永久不一致。
 * 正确实现应为 `!output.startsWith(prevOutput)`（与上一帧全文比前缀）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

const xtermMocks = vi.hoisted(() => ({
  instances: [] as Array<{
    loadAddon: ReturnType<typeof vi.fn>;
    open: ReturnType<typeof vi.fn>;
    dispose: ReturnType<typeof vi.fn>;
    clear: ReturnType<typeof vi.fn>;
    write: ReturnType<typeof vi.fn>;
    scrollToBottom: ReturnType<typeof vi.fn>;
  }>,
}));

// xterm 在测试环境无真实终端，替换为可观测桩；记录每个实例以供断言。
vi.mock("@xterm/xterm", () => ({
  Terminal: vi.fn().mockImplementation(() => {
    const instance = {
      loadAddon: vi.fn(),
      open: vi.fn(),
      dispose: vi.fn(),
      clear: vi.fn(),
      write: vi.fn(),
      scrollToBottom: vi.fn(),
    };
    xtermMocks.instances.push(instance);
    return instance;
  }),
}));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: vi.fn().mockImplementation(() => ({ fit: vi.fn() })),
}));
vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  logDebug: vi.fn(),
}));

import { TerminalViewer } from "@/components/chat/TerminalViewer";

describe("TerminalViewer output 替换检测", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    xtermMocks.instances.length = 0;
  });

  // 测试目的：同长度但内容全变的 output 替换（"AAAA"→"BBBB"）应触发清屏 + 全量重写。
  // 可能发现的缺陷：前缀检测析取支恒 false（死代码），同长度替换既不 clear 也不重写，
  //   终端残留旧内容 "AAAA"，新内容 "BBBB" 永不显示。
  it("output 同长度整体替换（AAAA→BBBB）时应清屏并重写新内容", () => {
    const { rerender } = render(<TerminalViewer output="AAAA" />);
    const term = xtermMocks.instances[0];
    // 前置确认：首帧增量写入正常（证明 mock 链路接通）。
    expect(term.write).toHaveBeenCalledWith("AAAA");

    rerender(<TerminalViewer output="BBBB" />);

    // 正确行为：检测到前缀已变 → clear + 写入完整 "BBBB"（两者缺一不可：
    // 「只重写未清屏」会残留旧内容，「只清屏不重写」会丢失新内容）。
    const cleared = term.clear.mock.calls.length > 0;
    const rewroteNewContent = term.write.mock.calls.some((call) => call[0] === "BBBB");
    expect(cleared && rewroteNewContent).toBe(true);
  });

  // 测试目的：正向对照——正常的尾部追加（"AAAA"→"AAAA-bb"）应增量写入切片。
  // 可能发现的缺陷：无（此用例应 PASS，证明增量写入主路径与 mock 均正常）。
  it("对照：尾部追加时增量写入新增切片（不清屏）", () => {
    const { rerender } = render(<TerminalViewer output="AAAA" />);
    const term = xtermMocks.instances[0];
    rerender(<TerminalViewer output="AAAA-bb" />);

    expect(term.clear).not.toHaveBeenCalled();
    expect(term.write).toHaveBeenLastCalledWith("-bb");
  });

  // 测试目的：正向对照——output 变短（回退/重置）时应清屏重写。
  // 可能发现的缺陷：无（此用例应 PASS，覆盖回退分支另一侧）。
  it("对照：output 变短（AAAA-bb→AA）时清屏并重写", () => {
    const { rerender } = render(<TerminalViewer output="AAAA-bb" />);
    const term = xtermMocks.instances[0];
    rerender(<TerminalViewer output="AA" />);

    expect(term.clear).toHaveBeenCalledTimes(1);
    expect(term.write).toHaveBeenLastCalledWith("AA");
  });
});
