import { isImeComposing } from "@/lib/keyboard/ime";
import { matchesShortcut } from "@/lib/keyboard/shortcuts";

export type ComposerEnterKeyEvent = {
  key: string;
  code: string;
  shiftKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  defaultPrevented: boolean;
  nativeEvent: { isComposing: boolean; keyCode: number };
};

/**
 * 一次提交要的并入方式：`send` 是默认发送（运行中即插话，下一步就看到），`queue` 是排到当前运行之后。
 * 只是用户意图，最终并入时机由后端结合 Session 阶段判定（空闲、等回答时排队也立即处理）。
 */
export type ComposerSubmitIntent = "send" | "queue";

/** 未捕获用户选区时从文档末尾插入；捕获后沿用编辑器当前选区锚点。 */
export function resolveComposerInsertionPosition(
  hasCapturedSelection: boolean,
  selectionAnchor: number,
  documentContentSize: number,
): number {
  return hasCapturedSelection ? selectionAnchor : Math.max(0, documentContentSize - 1);
}

/**
 * Enter 发送；⌘Enter（其他平台 Ctrl+Enter，快捷键 `queueMessage`）加入队列；Shift+Enter 与其余修饰键
 * 组合不提交（换行交给编辑器）；输入法组字期间的 Enter 只确认候选词（见 lib/keyboard/ime.ts）。
 */
export function composerSubmitIntent(event: ComposerEnterKeyEvent, isComposing: boolean): ComposerSubmitIntent | null {
  if (event.key !== "Enter" || event.defaultPrevented || isImeComposing(event, isComposing)) return null;
  if (matchesShortcut(event, "queueMessage")) return "queue";
  return event.shiftKey || event.metaKey || event.ctrlKey || event.altKey ? null : "send";
}
