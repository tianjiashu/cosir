import { isMac } from "@/lib/platform";

/**
 * 应用级快捷键的唯一登记处：按键（react-hotkeys-hook 语法，`mod` = macOS ⌘ / 其他平台 Ctrl）
 * 与展示文案都从这里取，tooltip 与命令面板显示的组合键因此与实际绑定一致。
 *
 * 绑定由 AppLayout / 命令面板通过 useHotkeys 注册；新增快捷键先在这里登记并检查
 * 与 WebView / 系统保留键（⌘Q、⌘W、⌘R 等）以及编辑器内快捷键不冲突。
 */
export const APP_SHORTCUTS = {
  newConversation: { keys: "mod+n", label: "新对话" },
  toggleSidebar: { keys: "mod+b", label: "切换侧栏" },
  openSettings: { keys: "mod+comma", label: "打开系统配置" },
  commandPalette: { keys: "mod+k", label: "命令面板" },
  // 与 VS Code 一致：所有平台都是 Ctrl+`（macOS 也不是 ⌘）。终端视图把它让给应用，不送入 shell。
  toggleTerminal: { keys: "ctrl+backquote", label: "切换终端" },
  // 与 VS Code 的辅助侧栏一致。
  toggleWorkbench: { keys: "mod+alt+b", label: "切换工作台" },
  // 只在 Markdown 配置面板挂载期间绑定。
  saveConfiguration: { keys: "mod+s", label: "保存" },
} as const;

export type AppShortcutId = keyof typeof APP_SHORTCUTS;

const KEY_LABELS: Record<string, string> = { comma: ",", period: ".", slash: "/", enter: "↵", escape: "Esc", backquote: "`" };

/** 把登记的按键转换为当前平台的展示形式，例如 macOS 上的「⌘N」、Windows 上的「Ctrl+N」。 */
export function shortcutLabel(id: AppShortcutId): string {
  const parts = APP_SHORTCUTS[id].keys.split("+").map((part) => {
    if (part === "mod") return isMac ? "⌘" : "Ctrl";
    if (part === "ctrl") return isMac ? "⌃" : "Ctrl";
    if (part === "shift") return isMac ? "⇧" : "Shift";
    if (part === "alt") return isMac ? "⌥" : "Alt";
    return KEY_LABELS[part] ?? part.toUpperCase();
  });
  return isMac ? parts.join("") : parts.join("+");
}

/** tooltip 文案：「新对话（⌘N）」。 */
export function withShortcut(label: string, id: AppShortcutId): string {
  return `${label}（${shortcutLabel(id)}）`;
}

/**
 * 焦点在交互终端（xterm）里时，Ctrl 组合键属于终端（Ctrl+N 下一行、Ctrl+B 后退、Ctrl+K 删到行尾），
 * 应用快捷键不得抢走；macOS 的应用快捷键用 ⌘，不受影响。各处 useHotkeys 以 `ignoreEventWhen` 接入。
 * 切换终端（Ctrl+`）不受此限，终端视图会把它让给应用。
 */
export function isTerminalKeyEvent(event: KeyboardEvent): boolean {
  if (!event.ctrlKey || event.code === "Backquote") return false;
  const target = event.target;
  return target instanceof Element && target.closest(".xterm") !== null;
}
