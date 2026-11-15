import { useRef } from "react";
import { useHotkeys } from "react-hotkeys-hook";

import type { HostPlatform } from "@/electron/shared/host-contract";
import { platform as currentPlatform } from "@/lib/platform";

/**
 * 全应用快捷键的唯一事实源：按键、作用范围、展示文案都只在这里写，绑定与展示经下面三个出口取用——
 * - 全局快捷键：`useAppHotkey(id, handler)`；
 * - 内层快捷键（编辑器、文件阅读器、终端、卡片等自己的 keydown）：`matchesShortcut(event, id)`；
 * - 展示：`shortcutLabel(id)` / `withShortcut(label, id)`。
 *
 * 优先级契约：内层先处理，处理了就 `preventDefault`；全局快捷键跳过已被处理的事件（`APP_HOTKEY_OPTIONS`）。
 * 内层与外层同键时必须在内层条目上写 `overrides` 声明「这里覆盖它」，否则冲突测试
 * （`tests/unit/keyboard-shortcuts.test.ts`）失败；保留键（系统、宿主菜单、Windows 的 AltGr）同样由它守住。
 *
 * 不在这里的按键：文字编辑语义（输入框回车发送、终端复制粘贴与中断）与控件标准交互（Esc、方向键、Tab）。
 * 它们的规则归各自的主人，不是「做某件事」的快捷键。
 *
 * 键位语法是 react-hotkeys-hook 的：`mod` = macOS ⌘ / 其他平台 Ctrl，键名取 `KeyboardEvent.code` 去掉
 * Key / Digit 前缀后的小写（按物理键位匹配，与全局监听一致）。平台只经 `lib/platform` 判断。
 */

export type ShortcutScope =
  | "global"
  | "configEditor"
  | "markdownEditor"
  | "fileReader"
  | "terminal"
  | "questionCard"
  | "modelPicker";

/**
 * 作用范围的嵌套：焦点在子范围里时，父范围的快捷键也在监听（全局监听挂在 document 上，覆盖一切）。
 * 兄弟范围不会同时获得焦点，同键不冲突。
 */
export const SCOPE_PARENT: Record<Exclude<ShortcutScope, "global">, ShortcutScope> = {
  configEditor: "global",
  markdownEditor: "global",
  fileReader: "global",
  terminal: "global",
  questionCard: "global",
  modelPicker: "global",
};

type ShortcutKeys = string | { mac: string; other: string };

type ShortcutDefinition = {
  keys: ShortcutKeys;
  label: string;
  scope: ShortcutScope;
  /** 内层与外层同键时，内层声明覆盖了哪几条（内层先处理并 preventDefault，外层随之跳过）。 */
  overrides?: readonly string[];
};

export const SHORTCUTS = {
  // ---- 全局 ----
  newConversation: { keys: "mod+n", label: "新会话", scope: "global" },
  toggleSidebar: { keys: "mod+b", label: "切换侧栏", scope: "global" },
  openSettings: { keys: "mod+comma", label: "打开系统配置", scope: "global" },
  commandPalette: { keys: "mod+k", label: "命令面板", scope: "global" },
  // 与 VS Code 一致：所有平台都是 Ctrl+`（macOS 的 ⌘` 是系统切换窗口）。终端把它让给应用，不送入 shell。
  toggleTerminal: { keys: "ctrl+backquote", label: "切换终端", scope: "global" },
  // macOS 与 VS Code 的辅助侧栏一致；Windows 不用 Ctrl+Alt：它等同 AltGr，欧洲键盘布局靠它打字符。
  toggleWorkbench: { keys: { mac: "mod+alt+b", other: "ctrl+shift+b" }, label: "切换工作台", scope: "global" },
  // Windows 不用 Ctrl+Shift+R：开发期视图菜单的强制刷新占着它。
  toggleConversationAside: { keys: { mac: "mod+alt+r", other: "ctrl+shift+period" }, label: "切换会话侧栏", scope: "global" },

  // ---- Markdown 配置面板（只在挂载期间绑定） ----
  saveConfiguration: { keys: "mod+s", label: "保存", scope: "configEditor" },

  // ---- Markdown 编辑器（`markdown-editor-keymap.ts`） ----
  markdownBold: { keys: "mod+b", label: "加粗", scope: "markdownEditor", overrides: ["toggleSidebar"] },
  markdownItalic: { keys: "mod+i", label: "斜体", scope: "markdownEditor" },
  markdownStrike: { keys: "mod+shift+x", label: "删除线", scope: "markdownEditor" },
  markdownLink: { keys: "mod+k", label: "插入链接", scope: "markdownEditor", overrides: ["commandPalette"] },
  // 不用 mod+`：macOS 的 ⌘` 是系统切换窗口，Windows 的 Ctrl+` 是切换终端。
  markdownCode: { keys: "mod+e", label: "行内代码", scope: "markdownEditor" },
  markdownOrderedList: { keys: "mod+shift+7", label: "有序列表", scope: "markdownEditor" },
  markdownBulletList: { keys: "mod+shift+8", label: "无序列表", scope: "markdownEditor" },
  markdownQuote: { keys: "mod+shift+9", label: "引用", scope: "markdownEditor" },

  // ---- 文件阅读器（`lib/workspace-files/reading-commands.ts`）；加到对话与 Cursor 的「Add to Chat」一致 ----
  findInFile: { keys: "mod+f", label: "查找", scope: "fileReader" },
  addToChat: { keys: "mod+l", label: "添加到对话", scope: "fileReader" },

  // ---- 交互终端（`lib/terminal/terminal-keyboard.ts`；复制、粘贴、中断是终端语义，不在这里） ----
  terminalSearch: { keys: "mod+f", label: "在终端中查找", scope: "terminal" },

  // ---- 对话里的卡片与选择器 ----
  questionSubmit: { keys: "mod+enter", label: "提交回答", scope: "questionCard" },
  selectAllModels: { keys: "mod+a", label: "显示全部模型", scope: "modelPicker" },
} as const satisfies Record<string, ShortcutDefinition>;

export type ShortcutId = keyof typeof SHORTCUTS;

/** 平台的按键习惯只分两类：macOS 用 ⌘，其余（Windows、Linux）用 Ctrl。 */
export function isMacKeyboard(target: HostPlatform): boolean {
  return target === "darwin";
}

/**
 * 条目在指定平台的具体键位：分平台的取对应一份，`mod` 换成 `meta` / `ctrl`。
 * 交给 react-hotkeys-hook 的也是这个结果，所以 `mod` 的含义只由 `lib/platform` 决定（库自己按 UA 判断）。
 */
export function resolveShortcutKeys(id: ShortcutId, target: HostPlatform = currentPlatform): string {
  const keys: ShortcutKeys = SHORTCUTS[id].keys;
  const mac = isMacKeyboard(target);
  const combo = typeof keys === "string" ? keys : mac ? keys.mac : keys.other;
  return combo.split("+").map((part) => (part === "mod" ? (mac ? "meta" : "ctrl") : part)).join("+");
}

type ShortcutKeyEvent = Pick<KeyboardEvent, "code" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey"> & {
  getModifierState?: (key: "AltGraph") => boolean;
};

/** `KeyboardEvent.code` → 键位语法里的键名，与 react-hotkeys-hook 的映射一致（KeyB → b、Digit7 → 7）。 */
function codeName(code: string): string {
  return code.toLowerCase().replace(/^(key|digit|numpad)/, "");
}

/**
 * 内层快捷键的判断：修饰键精确匹配（macOS 只认 ⌘、其他平台只认 Ctrl），主键按物理键位。
 * AltGr 输入的字符不算快捷键：Windows 上它同时带 Ctrl 与 Alt，不拦会吞掉字符。
 */
export function matchesShortcut(event: ShortcutKeyEvent, id: ShortcutId, target: HostPlatform = currentPlatform): boolean {
  if (event.getModifierState?.("AltGraph")) return false;
  const parts = resolveShortcutKeys(id, target).split("+");
  const key = parts.at(-1);
  return event.metaKey === parts.includes("meta")
    && event.ctrlKey === parts.includes("ctrl")
    && event.altKey === parts.includes("alt")
    && event.shiftKey === parts.includes("shift")
    && codeName(event.code) === key;
}

const KEY_LABELS: Record<string, string> = { comma: ",", period: ".", slash: "/", enter: "↵", escape: "Esc", backquote: "`" };

/**
 * 把键位语法的组合转换为指定平台的展示形式（macOS「⌘⇧B」、Windows「Ctrl+Shift+B」），平台符号只此一份。
 * 不在登记表里的按键（如输入框的回车发送，规则在 `components/composer/composer-editor-policy.ts`）也经这里展示。
 */
export function formatKeys(keys: string, target: HostPlatform = currentPlatform): string {
  const mac = isMacKeyboard(target);
  const parts = keys.split("+").map((part) => {
    if (part === "mod" || part === "meta") return mac ? "⌘" : part === "meta" ? "Win" : "Ctrl";
    if (part === "ctrl") return mac ? "⌃" : "Ctrl";
    if (part === "shift") return mac ? "⇧" : "Shift";
    if (part === "alt") return mac ? "⌥" : "Alt";
    return KEY_LABELS[part] ?? part.toUpperCase();
  });
  return mac ? parts.join("") : parts.join("+");
}

/** 登记的快捷键在当前平台的展示形式，例如 macOS 上的「⌘N」、Windows 上的「Ctrl+N」。 */
export function shortcutLabel(id: ShortcutId, target: HostPlatform = currentPlatform): string {
  return formatKeys(resolveShortcutKeys(id, target), target);
}

/** tooltip 文案：「新会话（⌘N）」。 */
export function withShortcut(label: string, id: ShortcutId): string {
  return `${label}（${shortcutLabel(id)}）`;
}

/**
 * 焦点在交互终端（xterm）里时，Ctrl 组合键属于终端（Ctrl+N 下一行、Ctrl+B 后退、Ctrl+K 删到行尾），
 * 应用快捷键不得抢走；macOS 的应用快捷键用 ⌘，不受影响。切换终端除外，终端视图会把它让给应用。
 */
function isTerminalKeyEvent(event: KeyboardEvent): boolean {
  if (!event.ctrlKey || matchesShortcut(event, "toggleTerminal")) return false;
  const target = event.target;
  return target instanceof Element && target.closest(".xterm") !== null;
}

/**
 * 全局快捷键的 useHotkeys 选项：输入框内同样生效；内层已处理（`defaultPrevented`）的事件跳过，这就是
 * 优先级契约的落点；终端里的 Ctrl 组合键让给终端。
 */
const APP_HOTKEY_OPTIONS = {
  enableOnFormTags: true,
  enableOnContentEditable: true,
  preventDefault: true,
  ignoreEventWhen: (event: KeyboardEvent) => event.defaultPrevented || isTerminalKeyEvent(event),
} as const;

/**
 * 注册一个全局快捷键（`scope` 为 global 或只在挂载期间生效的面板级条目）。处理函数每次渲染取最新的，
 * 调用方不用维护依赖数组。
 */
export function useAppHotkey(id: ShortcutId, handler: () => void, { enabled = true }: { enabled?: boolean } = {}) {
  const latest = useRef(handler);
  latest.current = handler;
  useHotkeys(resolveShortcutKeys(id), () => latest.current(), { ...APP_HOTKEY_OPTIONS, enabled });
}
