import type { KeyboardEvent } from "react";

/**
 * 用键盘打开右键菜单：菜单键或 Shift+F10（Windows / Linux 的标准做法）。WebView 对它们的原生行为不一致，
 * 统一在目标元素上派发一次 `contextmenu`，交给现有的 Radix 右键菜单。属于控件标准交互，不进快捷键登记表；
 * 判断只在 `isContextMenuKey` 一处。
 */
export function isContextMenuKey(event: KeyboardEvent<HTMLElement>): boolean {
  return event.key === "ContextMenu" || (event.shiftKey && event.key === "F10");
}

/** 在 `target` 上打开右键菜单（位置取元素左侧中部），并吞掉这次按键。调用方先用 `isContextMenuKey` 判断。 */
export function openContextMenuAt(event: KeyboardEvent<HTMLElement>, target: HTMLElement): void {
  event.preventDefault();
  event.stopPropagation();
  const bounds = target.getBoundingClientRect();
  target.dispatchEvent(
    new MouseEvent("contextmenu", {
      bubbles: true,
      cancelable: true,
      clientX: bounds.left + 20,
      clientY: bounds.top + bounds.height / 2,
    }),
  );
}

/** 焦点元素自己就是菜单目标时的便捷写法。 */
export function openKeyboardContextMenu(event: KeyboardEvent<HTMLElement>): void {
  if (isContextMenuKey(event)) openContextMenuAt(event, event.currentTarget);
}
