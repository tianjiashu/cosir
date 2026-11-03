/**
 * 交互终端的按键归属规则（纯函数，沿用 VS Code 约定）。
 *
 * - Windows / Linux：有选中文字时 Ctrl+C 复制，否则作为中断送入终端；Ctrl+Shift+C / V 恒为复制 / 粘贴。
 * - macOS：Cmd+C / Cmd+V 复制粘贴，Ctrl+C 恒为中断。
 * - 作为中断送入的 Ctrl+C 与其他按键一样走普通输入通道（后端从不拒绝用户输入）；作为复制的 Ctrl+C
 *   不产生任何输入，因此后端不会把它记为用户在打字、不会让 Agent 的输入等待。
 * - Ctrl/Cmd+F 打开面板内搜索；Ctrl+` 交给应用快捷键开关面板，不送入终端。
 * - 输入法组合中（IME）一律交给 xterm，不拦截。
 * - macOS 的行编辑键改发 shell 认识的序列（`{ send }`）：xterm 6 把 Option+方向键编成 `ESC[1;3D`，
 *   zsh / bash 默认没有绑定；Cmd+方向键与 Cmd+Backspace 则什么都不发。Windows 不翻译：Ctrl+方向键、
 *   Home / End 已编成标准序列，ConPTY 交给 PSReadLine / cmd 按词移动、到行首行尾，另造映射反而会和
 *   PSReadLine 自己的键表冲突。Option 不当 Meta（`macOptionIsMeta: false`），否则欧洲键盘布局打不出
 *   `@ { [`。
 *
 * 普通 Ctrl+V / Cmd+V 返回 `native-paste`：放行浏览器原生粘贴事件，由终端视图在 paste 事件上统一
 * 做粘贴确认；Ctrl+Shift+V（`paste`）读剪贴板后进入同一个确认入口。
 */
export type TerminalKeyAction = "terminal" | "copy" | "paste" | "native-paste" | "search" | "app" | { send: string };

export type TerminalKeyEvent = Pick<KeyboardEvent, "type" | "key" | "code" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey" | "isComposing">;

/**
 * macOS 行编辑键 → readline / ZLE 的 emacs 键位：按词移动、行首行尾；Cmd+Backspace 发 `^U`（bash 删到行首，
 * zsh 默认删整行），与 VS Code 在 macOS 上的默认键位一致。
 */
const MAC_OPTION_SEQUENCES: Record<string, string> = { ArrowLeft: "\x1bb", ArrowRight: "\x1bf" };
const MAC_COMMAND_SEQUENCES: Record<string, string> = { ArrowLeft: "\x01", ArrowRight: "\x05", Backspace: "\x15" };

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
    if (!event.ctrlKey && !event.shiftKey) {
      const sequence = event.altKey && !event.metaKey
        ? MAC_OPTION_SEQUENCES[event.key]
        : event.metaKey && !event.altKey
          ? MAC_COMMAND_SEQUENCES[event.key]
          : undefined;
      if (sequence !== undefined) return { send: sequence };
    }
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

/**
 * 一次粘贴的字节上限：取后端输入队列（`MAX_INPUT_QUEUE_BYTES`，按 base64 帧计）一定装得下的量。超过它
 * 会在队列中途背压，粘贴被截成半截执行，所以整段拒绝而不是截断发送。所有粘贴路径都要检查它。
 */
export const PASTE_MAX_BYTES = 256 * 1024;

/**
 * 粘贴是否需要确认：含换行，且前台程序没有开启 bracketed paste（`ESC[?2004h`）时才确认，对齐 VS Code
 * `enableMultiLinePasteWarning: auto`。开启时多行内容只进编辑缓冲、不会执行（zsh、bash ≥ 5.1 默认开启）；
 * 不开启时每一行都会被当作命令立即执行（macOS 自带 bash 3.2、cmd，Windows 上的 PSReadLine 大概率也
 * 属此类）。末尾的换行同样算：带换行的单行一粘贴就会执行。不看字数：不含换行的内容再长也不会自己执行。
 */
export function pasteNeedsConfirmation(text: string, { bracketedPasteMode }: { bracketedPasteMode: boolean }): boolean {
  return !bracketedPasteMode && /[\r\n]/.test(text);
}
