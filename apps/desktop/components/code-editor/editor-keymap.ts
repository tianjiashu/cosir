import { closeBracketsKeymap, completionKeymap, startCompletion } from "@codemirror/autocomplete";
import {
  addCursorAbove, addCursorBelow, copyLineDown, copyLineUp, cursorMatchingBracket, cursorSyntaxLeft, cursorSyntaxRight,
  deleteLine, historyKeymap, indentLess, indentMore, indentSelection, insertBlankLine, moveLineDown, moveLineUp, redo,
  redoSelection, selectLine, selectParentSyntax, selectSyntaxLeft, selectSyntaxRight, simplifySelection, standardKeymap, toggleBlockComment,
  toggleComment, toggleTabFocusMode, undo, undoSelection,
} from "@codemirror/commands";
import { foldAll, foldCode, unfoldAll, unfoldCode } from "@codemirror/language";
import { gotoLine, selectNextOccurrence, selectSelectionMatches } from "@codemirror/search";
import { Prec } from "@codemirror/state";
import { keymap, type Command, type KeyBinding } from "@codemirror/view";

import { codeMirrorKey } from "@/electron/shared/key-names";
import type { HostPlatform } from "@/electron/shared/host-contract";
import { resolveShortcutKeys, type ShortcutId } from "@/lib/keyboard/shortcuts";
import { platform as currentPlatform } from "@/lib/platform";

/**
 * 代码编辑器（CodeMirror）的功能键：每个命令的键位只取快捷键登记表（`lib/keyboard/shortcuts.ts`，作用范围
 * codeEditor），这里只把条目对上 CodeMirror 的命令。登记表因此看得见编辑器里的每一个功能键，冲突、保留键与
 * 平台规则由同一套守卫检查，提示文字也只经 `shortcutLabel` 生成。
 *
 * 不经登记表的只有文字编辑键与补全弹层里的键：它们是平台的输入习惯，不是「做某件事」的快捷键。
 *
 * 三层按优先级排定（同一个键先给上层，上层命令返回 false 才往下走），装配与守卫测试都只用这里导出的三层：
 * 1. 补全弹层（`POPUP_BINDINGS`，最高）：弹层开着时方向键选候选、回车确认、Esc 关闭；弹层没开时这些命令返回
 *    false，键落到下层。CodeMirror 默认也把它们放在最高优先级，排在标准键表后面会被光标移动、换行抢走。
 * 2. 功能键（`commandBindings`）：登记表生成。不强制阻止默认行为——只读视图里改文字的命令返回 false，按键
 *    照常交给外层，不被吞掉。
 * 3. 文字编辑键（`EDITING_BINDINGS`）：括号闭合（排在退格前）、Esc 收拢多选区、标准键表、撤销与重做。
 */
const COMMANDS = {
  goToLine: gotoLine,
  selectNextOccurrence,
  selectSelectionMatches,
  cursorSyntaxLeft,
  cursorSyntaxRight,
  selectSyntaxLeft,
  selectSyntaxRight,
  selectParentSyntax,
  selectLine,
  cursorMatchingBracket,
  addCursorAbove,
  addCursorBelow,
  moveLineUp,
  moveLineDown,
  copyLineUp,
  copyLineDown,
  deleteLine,
  insertBlankLine,
  indentLess,
  indentMore,
  indentSelection,
  toggleComment,
  toggleBlockComment,
  toggleTabFocusMode,
  undoSelection,
  redoSelection,
  foldCode,
  unfoldCode,
  foldAll,
  unfoldAll,
  startCompletion,
} satisfies Partial<Record<ShortcutId, Command>>;

export type EditorCommandId = keyof typeof COMMANDS;
export const EDITOR_COMMAND_IDS = Object.keys(COMMANDS) as EditorCommandId[];

const EDITING_HISTORY = new Set<Command>([undo, redo]);

/** 第一层：补全弹层（手动触发补全的键是功能键，在第二层）。 */
export const POPUP_BINDINGS: readonly KeyBinding[] = completionKeymap.filter((binding) => binding.run !== startCompletion);

/** 第二层：登记表里代码编辑器范围的功能键，按平台取具体键位。 */
export function commandBindings(target: HostPlatform = currentPlatform): KeyBinding[] {
  return EDITOR_COMMAND_IDS.map((id) => ({ key: codeMirrorKey(resolveShortcutKeys(id, target)), run: COMMANDS[id] }));
}

/** 第三层：文字编辑键。 */
export const EDITING_BINDINGS: readonly KeyBinding[] = [
  ...closeBracketsKeymap,
  { key: "Escape", run: simplifySelection },
  ...standardKeymap,
  ...historyKeymap.filter((binding) => binding.run && EDITING_HISTORY.has(binding.run)),
];

export const editorKeymap = [
  Prec.highest(keymap.of(POPUP_BINDINGS)),
  Prec.high(keymap.of(commandBindings())),
  keymap.of(EDITING_BINDINGS),
];
