import { describe, expect, it } from "vitest";

import { pasteNeedsConfirmation, resolveTerminalKeyAction, type TerminalKeyEvent } from "@/lib/terminal/terminal-keyboard";

function key(key: string, modifiers: Partial<TerminalKeyEvent> = {}): TerminalKeyEvent {
  return {
    type: "keydown",
    key,
    code: key.length === 1 ? `Key${key.toUpperCase()}` : key,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    altKey: false,
    isComposing: false,
    ...modifiers,
  };
}

const windows = (hasSelection: boolean) => ({ isMac: false, hasSelection });
const mac = (hasSelection: boolean) => ({ isMac: true, hasSelection });

describe("终端按键归属（VS Code 规则）", () => {
  it("Windows / Linux：有选中时 Ctrl+C 复制，否则作为中断送入终端", () => {
    expect(resolveTerminalKeyAction(key("c", { ctrlKey: true }), windows(true))).toBe("copy");
    expect(resolveTerminalKeyAction(key("c", { ctrlKey: true }), windows(false))).toBe("terminal");
  });

  it("Windows / Linux：Ctrl+Shift+C / V 恒为复制 / 粘贴，Ctrl+V 走原生粘贴", () => {
    expect(resolveTerminalKeyAction(key("C", { ctrlKey: true, shiftKey: true }), windows(false))).toBe("copy");
    expect(resolveTerminalKeyAction(key("V", { ctrlKey: true, shiftKey: true }), windows(false))).toBe("paste");
    expect(resolveTerminalKeyAction(key("v", { ctrlKey: true }), windows(false))).toBe("native-paste");
  });

  it("macOS：Cmd+C / Cmd+V 复制粘贴，Ctrl+C 恒为中断", () => {
    expect(resolveTerminalKeyAction(key("c", { metaKey: true }), mac(true))).toBe("copy");
    expect(resolveTerminalKeyAction(key("v", { metaKey: true }), mac(false))).toBe("native-paste");
    expect(resolveTerminalKeyAction(key("c", { ctrlKey: true }), mac(true))).toBe("terminal");
  });

  it("其他 Ctrl 组合键（Ctrl+N / B / K）留给终端；Ctrl+` 让给应用开关面板", () => {
    for (const letter of ["n", "b", "k", "r", "d"]) {
      expect(resolveTerminalKeyAction(key(letter, { ctrlKey: true }), windows(false))).toBe("terminal");
      expect(resolveTerminalKeyAction(key(letter, { ctrlKey: true }), mac(false))).toBe("terminal");
    }
    expect(resolveTerminalKeyAction(key("`", { ctrlKey: true, code: "Backquote" }), windows(false))).toBe("app");
    expect(resolveTerminalKeyAction(key("`", { ctrlKey: true, code: "Backquote" }), mac(false))).toBe("app");
  });

  it("Ctrl/Cmd+F 打开面板内搜索", () => {
    expect(resolveTerminalKeyAction(key("f", { ctrlKey: true }), windows(false))).toBe("search");
    expect(resolveTerminalKeyAction(key("f", { metaKey: true }), mac(false))).toBe("search");
    expect(resolveTerminalKeyAction(key("f", { ctrlKey: true }), mac(false))).toBe("terminal");
  });

  it("输入法组合期间一律交给 xterm", () => {
    expect(resolveTerminalKeyAction(key("c", { ctrlKey: true, isComposing: true }), windows(true))).toBe("terminal");
  });

  // 目的：macOS 上按词移动、行首行尾、删到行首可用；潜在缺陷：xterm 6 对这些键发 zsh / bash 不认的序列或什么都不发。
  it("macOS：Option / Cmd 行编辑键翻译成 shell 的 emacs 键位", () => {
    expect(resolveTerminalKeyAction(key("ArrowLeft", { altKey: true }), mac(false))).toEqual({ send: "\x1bb" });
    expect(resolveTerminalKeyAction(key("ArrowRight", { altKey: true }), mac(false))).toEqual({ send: "\x1bf" });
    expect(resolveTerminalKeyAction(key("ArrowLeft", { metaKey: true }), mac(false))).toEqual({ send: "\x01" });
    expect(resolveTerminalKeyAction(key("ArrowRight", { metaKey: true }), mac(false))).toEqual({ send: "\x05" });
    expect(resolveTerminalKeyAction(key("Backspace", { metaKey: true }), mac(false))).toEqual({ send: "\x15" });
  });

  it("macOS：带 Shift / Ctrl 的组合与其他键不翻译，交给 xterm 原样编码", () => {
    expect(resolveTerminalKeyAction(key("ArrowLeft", { altKey: true, shiftKey: true }), mac(false))).toBe("terminal");
    expect(resolveTerminalKeyAction(key("ArrowLeft", { metaKey: true, ctrlKey: true }), mac(false))).toBe("terminal");
    expect(resolveTerminalKeyAction(key("ArrowUp", { altKey: true }), mac(false))).toBe("terminal");
    expect(resolveTerminalKeyAction(key("Backspace", { altKey: true }), mac(false))).toBe("terminal");
  });

  // 目的：Windows 的行编辑交给 ConPTY + PSReadLine / cmd 自己的键表；潜在缺陷：另造映射与 PSReadLine 冲突。
  it("Windows：行编辑键不翻译", () => {
    expect(resolveTerminalKeyAction(key("ArrowLeft", { ctrlKey: true }), windows(false))).toBe("terminal");
    expect(resolveTerminalKeyAction(key("ArrowLeft", { altKey: true }), windows(false))).toBe("terminal");
    expect(resolveTerminalKeyAction(key("Backspace", { ctrlKey: true }), windows(false))).toBe("terminal");
  });
});

describe("粘贴确认（对齐 VS Code enableMultiLinePasteWarning: auto）", () => {
  const plain = { bracketedPasteMode: false };
  const bracketed = { bracketedPasteMode: true };

  // 目的：只在内容会被逐行执行时打扰；潜在缺陷：zsh 默认开启 bracketed paste 时仍弹确认，或 bash 3.2 / cmd 下多行直接执行。
  it("含换行且没开 bracketed paste 才需要确认，末尾换行也算", () => {
    expect(pasteNeedsConfirmation("ls -la", plain)).toBe(false);
    expect(pasteNeedsConfirmation("x".repeat(5000), plain)).toBe(false);
    expect(pasteNeedsConfirmation("ls -la\n", plain)).toBe(true);
    expect(pasteNeedsConfirmation("a\r\nb", plain)).toBe(true);
    expect(pasteNeedsConfirmation("a\rb", plain)).toBe(true);
  });

  it("开了 bracketed paste 时多行只进编辑缓冲，不确认", () => {
    expect(pasteNeedsConfirmation("a\nb\nc", bracketed)).toBe(false);
  });
});
