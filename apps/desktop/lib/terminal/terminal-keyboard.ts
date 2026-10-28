/**
 * 交互终端的按键归属规则（纯函数，沿用 VS Code 约定）。
 *
 * - Windows / Linux：有选中文字时 Ctrl+C 复制，否则作为中断送入终端；Ctrl+Shift+C / V 恒为复制 / 粘贴。
 * - macOS：Cmd+C / Cmd+V 复制粘贴，Ctrl+C 恒为中断。
 * - 作为中断送入的 Ctrl+C 走普通输入通道（后端租约的「Ctrl-C 例外」负责送达）；作为复制的 Ctrl+C
 *   不产生任何输入，因此不触碰租约。
 * - Ctrl/Cmd+F 打开面板内搜索；Ctrl+` 交给应用快捷键开关面板，不送入终端。
 * - 输入法组合中（IME）一律交给 xterm，不拦截。
 *
 * 普通 Ctrl+V / Cmd+V 返回 `native-paste`：放行浏览器原生粘贴事件，由终端视图在 paste 事件上统一
 * 做大段粘贴确认，菜单粘贴与快捷键粘贴走同一条路径。
 */
export type TerminalKeyAction = "terminal" | "copy" | "paste" | "native-paste" | "search" | "app";

export type TerminalKeyEvent = Pick<KeyboardEvent, "type" | "key" | "code" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey" | "isComposing">;

export function resolveTerminalKeyAction(
  event: TerminalKeyEvent,
  { isMac, hasSelection }: { isMac: boolean; hasSelection: boolean },
): TerminalKeyAction {
  if (event.isComposing) return "terminal";
  const key = event.key.toLowerCase();
  if (event.ctrlKey && !event.metaKey && !event.altKey && event.code === "Backquote") return "app";
  const commandKey = isMac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
  if (commandKey && !event.altKey && !event.shiftKey && key === "f") return "search";
  if (isMac) {
    if (!event.metaKey || event.ctrlKey || event.altKey) return "terminal";
    if (key === "c") return hasSelection ? "copy" : "app";
    if (key === "v") return "native-paste";
    return "terminal";
  }
  if (!event.ctrlKey || event.metaKey || event.altKey) return "terminal";
  if (event.shiftKey && key === "c") return "copy";
  if (event.shiftKey && key === "v") return "paste";
  if (!event.shiftKey && key === "c") return hasSelection ? "copy" : "terminal";
  if (!event.shiftKey && key === "v") return "native-paste";
  return "terminal";
}

/** 大段粘贴确认阈值（设计文档「二、已定参数」）：超过 1000 字符或 10 行。 */
const PASTE_CONFIRM_MAX_CHARS = 1000;
const PASTE_CONFIRM_MAX_LINES = 10;

export function pasteNeedsConfirmation(text: string): boolean {
  if (text.length > PASTE_CONFIRM_MAX_CHARS) return true;
  return text.split(/\r\n|\r|\n/).length > PASTE_CONFIRM_MAX_LINES;
}
