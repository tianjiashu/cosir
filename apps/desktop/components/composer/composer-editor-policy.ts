import { isImeComposing } from "@/lib/keyboard/ime";

export type ComposerEnterKeyEvent = {
  key: string;
  shiftKey: boolean;
  defaultPrevented: boolean;
  nativeEvent: { isComposing: boolean; keyCode: number };
};

/** 未捕获用户选区时从文档末尾插入；捕获后沿用编辑器当前选区锚点。 */
export function resolveComposerInsertionPosition(
  hasCapturedSelection: boolean,
  selectionAnchor: number,
  documentContentSize: number,
): number {
  return hasCapturedSelection ? selectionAnchor : Math.max(0, documentContentSize - 1);
}

/** Enter 发送；Shift+Enter 换行；输入法组字期间的 Enter 只确认候选词（见 lib/keyboard/ime.ts）。 */
export function shouldSubmitOnEnter(event: ComposerEnterKeyEvent, isComposing: boolean): boolean {
  return event.key === "Enter"
    && !event.shiftKey
    && !event.defaultPrevented
    && !isImeComposing(event, isComposing);
}
