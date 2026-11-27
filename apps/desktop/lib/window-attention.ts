/**
 * 「用户此刻在看这个窗口」的唯一定义：窗口可见且有焦点。藏到托盘、最小化、切到别的应用都不算。
 *
 * 为什么不只看 `document.visibilityState`：桌面应用切到别的应用时窗口通常仍然可见，可见性不变；只认它
 * 就分不出「用户走开又回来」。会话已读与查询的窗口聚焦重读都从这里取，两处对「回到前台」的理解一致。
 */
export function windowAttentive(): boolean {
  return document.visibilityState === "visible" && document.hasFocus();
}

/** 注视状态可能变化时回调（获得或失去焦点、可见性变化）；返回取消订阅。回调里用 {@link windowAttentive} 取当前值。 */
export function onWindowAttentionChange(listener: () => void): () => void {
  window.addEventListener("focus", listener);
  window.addEventListener("blur", listener);
  document.addEventListener("visibilitychange", listener);
  return () => {
    window.removeEventListener("focus", listener);
    window.removeEventListener("blur", listener);
    document.removeEventListener("visibilitychange", listener);
  };
}
