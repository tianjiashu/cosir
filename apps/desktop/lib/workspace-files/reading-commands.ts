import { createContext, useContext, useEffect, useRef } from "react";

import type { ShortcutId } from "@/lib/keyboard/shortcuts";

/**
 * 文件阅读器的快捷命令：查找、查找下一处 / 上一处（查找条开着时）、添加到对话（按键登记在 `lib/keyboard/shortcuts.ts`，作用范围 fileReader）。
 *
 * 只在文件面板内生效：`FileReadingTabs` 在捕获阶段拦下按键（焦点在阅读器的编辑器、工具栏或查找条里都能用），交给当前
 * 活动阅读器注册的动作；没有注册时按键照常传递。同一时刻只挂载活动文件的阅读器，所以每种命令至多一个动作。
 */
export const READING_COMMANDS = ["find", "findNext", "findPrevious", "addToChat"] as const;
export type ReadingCommand = (typeof READING_COMMANDS)[number];
/** 每个命令对应的登记条目。 */
export const READING_COMMAND_SHORTCUTS: Record<ReadingCommand, ShortcutId> = { find: "findInFile", findNext: "findNext", findPrevious: "findPrevious", addToChat: "addToChat" };
export type ReadingCommandRegistry = (command: ReadingCommand, action: (() => void) | null) => void;

export const ReadingCommandsContext = createContext<ReadingCommandRegistry | null>(null);

/** 挂载期间把 `command` 交给 `action`，卸载即注销。必须在 `FileReadingTabs` 之内使用。 */
export function useReadingCommand(command: ReadingCommand, action: () => void) {
  const register = useContext(ReadingCommandsContext)!;
  const latest = useRef(action);
  latest.current = action;
  useEffect(() => {
    register(command, () => latest.current());
    return () => register(command, null);
  }, [register, command]);
}
