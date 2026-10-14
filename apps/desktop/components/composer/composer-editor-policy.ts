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

/** 用 WebView 与浏览器 IME 信号共同阻止候选确认键误触发消息发送。 */
export function shouldSubmitOnEnter(event: ComposerEnterKeyEvent, isComposing: boolean): boolean {
  return event.key === "Enter"
    && !event.shiftKey
    && !event.defaultPrevented
    && !isComposing
    && !event.nativeEvent.isComposing
    && event.nativeEvent.keyCode !== 229;
}
