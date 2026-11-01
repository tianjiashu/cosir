import { createContext, useContext, useEffect, useRef } from "react";

/**
 * 文件阅读器的快捷命令：Ctrl/Cmd+F 查找、Ctrl/Cmd+L 添加到对话（按键登记在 `APP_SHORTCUTS`）。
 *
 * 只在文件面板内生效：`FileReadingTabs` 在捕获阶段拦下按键（抢在 CodeMirror 自带的查找键位之前），交给当前
 * 活动阅读器注册的动作；没有注册时按键照常传递。同一时刻只挂载活动文件的阅读器，所以每种命令至多一个动作。
 */
export type ReadingCommand = "find" | "addToChat";
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
