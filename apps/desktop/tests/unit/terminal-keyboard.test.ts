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
});

describe("大段粘贴确认", () => {
  it("超过 1000 字符或 10 行才需要确认", () => {
    expect(pasteNeedsConfirmation("ls -la")).toBe(false);
    expect(pasteNeedsConfirmation("x".repeat(1000))).toBe(false);
    expect(pasteNeedsConfirmation("x".repeat(1001))).toBe(true);
    expect(pasteNeedsConfirmation(Array.from({ length: 10 }, (_, i) => `l${i}`).join("\n"))).toBe(false);
    expect(pasteNeedsConfirmation(Array.from({ length: 11 }, (_, i) => `l${i}`).join("\r\n"))).toBe(true);
  });
});
