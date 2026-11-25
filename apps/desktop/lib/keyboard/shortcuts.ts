import { useRef } from "react";
import { useHotkeys } from "react-hotkeys-hook";

import type { HostPlatform } from "@/electron/shared/host-contract";
import { keyLabel } from "@/electron/shared/key-names";
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
 * 内置浏览器的网页里不生效（有意的，不是缺陷）：焦点在网页（宿主的 `WebContentsView`）里时，按键归网页，
 * 这里的快捷键不转发。网页自己常用同样的键（⌘K 搜索、⌘B 加粗），转发就会让网站的快捷键在内置浏览器里失灵，
 * 用户与 Agent 都在上面操作真实网站；与删掉「视图」菜单（`electron/main/app-menu.ts`：网页里按 ⌘R 却刷新了
 * 整个应用）是同一条原则。那里仍然生效的只有宿主菜单的键（退出、隐藏、最小化、关窗口，系统级约定）；
 * 要用应用快捷键，先把焦点移出网页。
 *
 * 键位语法是 react-hotkeys-hook 的：`mod` = macOS ⌘ / 其他平台 Ctrl，键名取 `KeyboardEvent.code` 去掉
 * Key / Digit 前缀后的小写（按物理键位匹配，与全局监听一致）。平台只经 `lib/platform` 判断。
 *
 * 三个平台各自成立：macOS、Windows、Linux 的按键习惯不同，键位按 `{ mac, other, linux? }` 分写，守卫测试
 * 对三个平台逐一检查保留键（宿主菜单与系统占用的，事实源在 `electron/shared/app-menu-keys.ts`）与输入冲突。
 * 第三方组件自带的键位（CodeMirror）也从这里生成（`components/code-editor/editor-keymap.ts`），不另留一套。
 */

export type ShortcutScope =
  | "global"
  | "configEditor"
  | "markdownEditor"
  | "fileReader"
  | "textSurface"
  | "codeEditor"
  | "replaceInput"
  | "terminal"
  | "questionCard"
  | "modelPicker"
  | "composer";

/**
 * 作用范围的嵌套：焦点在子范围里时，父范围的快捷键也在监听（全局监听挂在 document 上，覆盖一切）。
 * 兄弟范围不会同时获得焦点，同键不冲突。一个范围可能出现在几处：代码编辑器（CodeMirror）在文件阅读器里，
 * 也在观测页、Skill 详情这类独立文本面里；Markdown 配置编辑器本身就是一个代码编辑器。
 */
export const SCOPE_PARENTS: Record<Exclude<ShortcutScope, "global">, readonly ShortcutScope[]> = {
  configEditor: ["global"],
  fileReader: ["global"],
  textSurface: ["global"],
  codeEditor: ["fileReader", "textSurface", "configEditor"],
  markdownEditor: ["codeEditor"],
  // 查找条的替换框：焦点在输入框里，与代码编辑器是兄弟（同在独立文本面里，不同时获得焦点）。
  replaceInput: ["textSurface"],
  terminal: ["global"],
  questionCard: ["global"],
  modelPicker: ["global"],
  composer: ["global"],
};

/** 条目在哪些范围监听。 */
export function shortcutScopes(id: ShortcutId): readonly ShortcutScope[] {
  const scope: ShortcutScope | readonly ShortcutScope[] = SHORTCUTS[id].scope;
  return typeof scope === "string" ? [scope] : scope;
}

/** 范围连同它所有可能的外层（含全局），冲突检查按它判断「同时在监听」。 */
export function scopeAncestry(scope: ShortcutScope): ReadonlySet<ShortcutScope> {
  const result = new Set<ShortcutScope>([scope]);
  if (scope !== "global") for (const parent of SCOPE_PARENTS[scope]) for (const outer of scopeAncestry(parent)) result.add(outer);
  return result;
}

/** 键位：三个平台相同时写一个字符串；不同时分写，`other` 是 Windows 与 Linux 共用，`linux` 只在 Linux 不同时给。 */
type ShortcutKeys = string | { mac: string; other: string; linux?: string };

type ShortcutDefinition = {
  keys: ShortcutKeys;
  label: string;
  /** 在哪些范围监听；同一个操作在几个兄弟范围里语义相同时写成数组（如查找下一处）。 */
  scope: ShortcutScope | readonly ShortcutScope[];
  /** 内层与外层同键时，内层声明覆盖了哪几条（内层先处理并 preventDefault，外层随之跳过）。 */
  overrides?: readonly string[];
};

export const SHORTCUTS = {
  // ---- 全局 ----
  newConversation: { keys: "mod+n", label: "新会话", scope: "global" },
  toggleSidebar: { keys: "mod+b", label: "切换侧栏", scope: "global" },
  openConfiguration: { keys: "mod+comma", label: "打开系统配置", scope: "global" },
  commandPalette: { keys: "mod+k", label: "命令面板", scope: "global" },
  // 与 Cursor 的「Add to Chat」一致：有选区时打开摘录评论框（`components/text-selection`）；文件阅读器里没有选区时
  // 添加整个文件（阅读器在捕获阶段先接住，见 `reading-commands`）。
  addToChat: { keys: "mod+l", label: "添加到对话", scope: "global" },
  // 与 VS Code 一致：所有平台都是 Ctrl+`（macOS 的 ⌘` 是系统切换窗口）。终端把它让给应用，不送入 shell。
  toggleTerminal: { keys: "ctrl+backquote", label: "切换终端", scope: "global" },
  // macOS 与 VS Code 的辅助侧栏一致；Windows 与 Linux 不用 Ctrl+Alt（Windows 上即 AltGr，Linux 桌面环境占用）。
  toggleWorkbench: { keys: { mac: "mod+alt+b", other: "ctrl+shift+b" }, label: "切换工作台", scope: "global" },
  // Windows 不用 Ctrl+Shift+R：开发期视图菜单的强制刷新占着它。
  toggleConversationAside: { keys: { mac: "mod+alt+r", other: "ctrl+shift+period" }, label: "切换会话侧栏", scope: "global" },

  // ---- 单文本配置文档面板（Markdown 文档与 .fileignore，只在挂载期间绑定） ----
  saveConfiguration: { keys: "mod+s", label: "保存", scope: "configEditor" },

  // ---- Markdown 编辑器（`markdown-editor-keymap.ts`） ----
  markdownBold: { keys: "mod+b", label: "加粗", scope: "markdownEditor", overrides: ["toggleSidebar"] },
  markdownItalic: { keys: "mod+i", label: "斜体", scope: "markdownEditor", overrides: ["selectParentSyntax"] },
  markdownStrike: { keys: "mod+shift+x", label: "删除线", scope: "markdownEditor" },
  markdownLink: { keys: "mod+k", label: "插入链接", scope: "markdownEditor", overrides: ["commandPalette"] },
  // 不用 mod+`：macOS 的 ⌘` 是系统切换窗口，Windows 的 Ctrl+` 是切换终端。
  markdownCode: { keys: "mod+e", label: "行内代码", scope: "markdownEditor" },
  markdownOrderedList: { keys: "mod+shift+7", label: "有序列表", scope: "markdownEditor" },
  markdownBulletList: { keys: "mod+shift+8", label: "无序列表", scope: "markdownEditor" },
  markdownQuote: { keys: "mod+shift+9", label: "引用", scope: "markdownEditor" },

  // ---- 文件阅读器（`lib/workspace-files/reading-commands.ts`） ----
  findInFile: { keys: "mod+f", label: "查找", scope: "fileReader" },

  // ---- 独立文本面（观测页详情、Skill 详情、配置编辑器的源码；`components/code-editor/editor-find-scope.tsx`） ----
  findInText: { keys: "mod+f", label: "查找", scope: "textSurface" },
  // 替换只给可编辑的编辑器（查找对象提供替换能力时才响应）。macOS 与 VS Code 一致；Windows / Linux 用 Ctrl+H。
  openReplace: { keys: { mac: "mod+alt+f", other: "ctrl+h" }, label: "替换", scope: "textSurface" },
  // 替换框里回车替换当前处（控件标准交互），这个组合全部替换（`components/ui/find-bar.tsx`）。
  replaceAll: { keys: "mod+enter", label: "全部替换", scope: "replaceInput" },

  // ---- 查找条打开时（文件阅读器与独立文本面；终端不响应：F3 在终端里属于 shell） ----
  // Windows / Linux 不用 Ctrl+G：那是跳转到行（与 VS Code 一致）。
  findNext: { keys: { mac: "mod+g", other: "f3" }, label: "查找下一个", scope: ["fileReader", "textSurface"] },
  findPrevious: { keys: { mac: "mod+shift+g", other: "shift+f3" }, label: "查找上一个", scope: ["fileReader", "textSurface"] },

  // ---- 代码编辑器（CodeMirror，`components/code-editor/editor-keymap.ts` 由这些条目生成键位） ----
  // 只登记功能键；光标移动、选区、删除、撤销重做是文字编辑键，沿用 CodeMirror 的标准键表与编辑菜单。
  // 键位默认沿用 CodeMirror 的，改动处都是它的默认键与平台冲突：Windows / Linux 不用 Ctrl+Alt，macOS 不用
  // Option 配可打字的键，也不用系统占着的 ⌃方向键（调度中心）与 ⌃空格（输入法）。只读视图里改文字的命令不生效。
  goToLine: { keys: "ctrl+g", label: "跳转到行", scope: "codeEditor" },
  selectNextOccurrence: { keys: "mod+d", label: "选中下一个相同内容", scope: "codeEditor" },
  selectSelectionMatches: { keys: "mod+shift+l", label: "选中全部相同内容", scope: "codeEditor" },
  cursorSyntaxLeft: { keys: { mac: "ctrl+alt+arrowleft", other: "alt+arrowleft" }, label: "按语法单元左移", scope: "codeEditor" },
  cursorSyntaxRight: { keys: { mac: "ctrl+alt+arrowright", other: "alt+arrowright" }, label: "按语法单元右移", scope: "codeEditor" },
  selectSyntaxLeft: { keys: { mac: "ctrl+alt+shift+arrowleft", other: "alt+shift+arrowleft" }, label: "按语法单元向左选择", scope: "codeEditor" },
  selectSyntaxRight: { keys: { mac: "ctrl+alt+shift+arrowright", other: "alt+shift+arrowright" }, label: "按语法单元向右选择", scope: "codeEditor" },
  selectParentSyntax: { keys: "mod+i", label: "扩大选区到外层语法", scope: "codeEditor" },
  selectLine: { keys: { mac: "ctrl+l", other: "alt+l" }, label: "选中整行", scope: "codeEditor" },
  cursorMatchingBracket: { keys: "mod+shift+backslash", label: "跳到匹配括号", scope: "codeEditor" },
  addCursorAbove: { keys: { mac: "mod+alt+arrowup", other: "ctrl+shift+arrowup" }, label: "在上方添加光标", scope: "codeEditor" },
  addCursorBelow: { keys: { mac: "mod+alt+arrowdown", other: "ctrl+shift+arrowdown" }, label: "在下方添加光标", scope: "codeEditor" },
  moveLineUp: { keys: "alt+arrowup", label: "上移行", scope: "codeEditor" },
  moveLineDown: { keys: "alt+arrowdown", label: "下移行", scope: "codeEditor" },
  copyLineUp: { keys: "alt+shift+arrowup", label: "向上复制行", scope: "codeEditor" },
  copyLineDown: { keys: "alt+shift+arrowdown", label: "向下复制行", scope: "codeEditor" },
  deleteLine: { keys: "mod+shift+k", label: "删除行", scope: "codeEditor" },
  insertBlankLine: { keys: "mod+enter", label: "在下方插入空行", scope: "codeEditor" },
  indentLess: { keys: "mod+bracketleft", label: "减少缩进", scope: "codeEditor" },
  indentMore: { keys: "mod+bracketright", label: "增加缩进", scope: "codeEditor" },
  indentSelection: { keys: { mac: "mod+alt+backslash", other: "alt+shift+f" }, label: "重新缩进选区", scope: "codeEditor" },
  toggleComment: { keys: "mod+slash", label: "切换行注释", scope: "codeEditor" },
  toggleBlockComment: { keys: { mac: "mod+shift+a", other: "alt+shift+a" }, label: "切换块注释", scope: "codeEditor" },
  // Tab 键改为移动焦点（读屏用户离开编辑器）；CodeMirror 默认的 Ctrl+M 在 Windows / Linux 是菜单的最小化。
  toggleTabFocusMode: { keys: "ctrl+shift+m", label: "Tab 键移动焦点", scope: "codeEditor" },
  undoSelection: { keys: "mod+u", label: "撤销选区变化", scope: "codeEditor" },
  redoSelection: { keys: { mac: "mod+shift+u", other: "alt+u" }, label: "重做选区变化", scope: "codeEditor" },
  foldCode: { keys: { mac: "mod+alt+bracketleft", other: "ctrl+shift+bracketleft" }, label: "折叠", scope: "codeEditor" },
  unfoldCode: { keys: { mac: "mod+alt+bracketright", other: "ctrl+shift+bracketright" }, label: "展开", scope: "codeEditor" },
  foldAll: { keys: { mac: "ctrl+alt+bracketleft", other: "alt+shift+bracketleft" }, label: "全部折叠", scope: "codeEditor" },
  unfoldAll: { keys: { mac: "ctrl+alt+bracketright", other: "alt+shift+bracketright" }, label: "全部展开", scope: "codeEditor" },
  // Windows 中文输入法与 Linux IBus 常用 Ctrl+空格切换输入法，macOS 的 ⌃空格同样是切换输入法。
  startCompletion: { keys: { mac: "alt+escape", other: "ctrl+shift+space" }, label: "触发补全", scope: "codeEditor" },

  // ---- 交互终端（`lib/terminal/terminal-keyboard.ts`；复制、粘贴、中断是终端语义，不在这里） ----
  terminalSearch: { keys: "mod+f", label: "在终端中查找", scope: "terminal" },
  // 恒定的复制 / 粘贴键（VS Code 约定）：Windows 与 Linux 的 Ctrl+C / Ctrl+V 属于 shell（中断、字面输入）。
  terminalCopy: { keys: { mac: "mod+c", other: "ctrl+shift+c" }, label: "复制", scope: "terminal" },
  terminalPaste: { keys: { mac: "mod+v", other: "ctrl+shift+v" }, label: "粘贴", scope: "terminal" },

  // ---- 对话里的卡片与选择器 ----
  questionSubmit: { keys: "mod+enter", label: "提交回答", scope: "questionCard" },
  // 对话输入框：回车发送（运行中即插话）是文字编辑语义，不在这里；⌘回车是另一件事——排到当前运行之后。
  queueMessage: { keys: "mod+enter", label: "加入队列", scope: "composer" },
  selectAllModels: { keys: "mod+a", label: "全选当前搜索结果", scope: "modelPicker" },
} as const satisfies Record<string, ShortcutDefinition>;

export type ShortcutId = keyof typeof SHORTCUTS;

/** 主修饰键：macOS 用 ⌘，Windows 与 Linux 用 Ctrl。 */
export function isMacKeyboard(target: HostPlatform): boolean {
  return target === "darwin";
}

/** 条目在指定平台的键位语法（仍含 `mod`）：分平台的取对应一份。 */
export function shortcutCombo(id: ShortcutId, target: HostPlatform = currentPlatform): string {
  const keys: ShortcutKeys = SHORTCUTS[id].keys;
  if (typeof keys === "string") return keys;
  if (isMacKeyboard(target)) return keys.mac;
  return target === "linux" ? keys.linux ?? keys.other : keys.other;
}

/**
 * 条目在指定平台的具体键位：`mod` 换成 `meta` / `ctrl`。
 * 交给 react-hotkeys-hook 的也是这个结果，所以 `mod` 的含义只由 `lib/platform` 决定（库自己按 UA 判断）。
 */
export function resolveShortcutKeys(id: ShortcutId, target: HostPlatform = currentPlatform): string {
  const mac = isMacKeyboard(target);
  return shortcutCombo(id, target).split("+").map((part) => (part === "mod" ? (mac ? "meta" : "ctrl") : part)).join("+");
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

/**
 * 把键位语法的组合转换为指定平台的展示形式（macOS「⌘⇧B」、Windows「Ctrl+Shift+B」）；各键的写法只在
 * `electron/shared/key-names.ts`。
 * 不在登记表里的按键（如输入框的回车发送，规则在 `components/composer/composer-editor-policy.ts`）也经这里展示。
 */
export function formatKeys(keys: string, target: HostPlatform = currentPlatform): string {
  const parts = keys.split("+").map((part) => keyLabel(part, target));
  return isMacKeyboard(target) ? parts.join("") : parts.join("+");
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
