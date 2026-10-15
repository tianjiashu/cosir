/**
 * 输入法（IME）组字期间的按键判断——所有「Enter 提交 / 确认」处理的统一前置检查。
 *
 * 中文等输入法按 Enter 确认候选词时，各 WebView 给出的信号不一致：WKWebView 可能先触发
 * compositionend 再送出 `isComposing=false` 的 Enter，但其 keyCode 仍为 229。因此同时检查
 * 原生 `isComposing`、keyCode 229，以及调用方用 compositionstart/end 维护的组字标记。
 * 只判断 `isComposing` 会在 WKWebView 上把候选确认误当成提交。
 */
type ImeSignals = { isComposing: boolean; keyCode: number };

/** React 合成事件（读 nativeEvent）与原生 / CodeMirror KeyboardEvent 均可直接传入。 */
export type ImeKeyEvent = ImeSignals | { nativeEvent: ImeSignals };

export function isImeComposing(event: ImeKeyEvent, composingFlag = false): boolean {
  const signals = "nativeEvent" in event ? event.nativeEvent : event;
  return composingFlag || signals.isComposing || signals.keyCode === 229;
}
