import { useHotkeys } from "react-hotkeys-hook";

import { APP_SHORTCUTS } from "@/lib/shortcuts";

/**
 * 绑定 ⌘K / Ctrl+K 打开命令面板。输入框与富文本编辑器内同样生效（该组合键不与文本编辑冲突），
 * 并阻止 WebView 默认行为。
 *
 * useHotkeys 默认以空依赖 memo 回调，因此必须把 `onOpen` 作为依赖传入，否则会一直调用首次渲染的闭包；
 * 调用方传入稳定引用（如 `useCallback` 或 state setter）可避免每次渲染重绑监听。
 */
export function useCommandPaletteHotkey(onOpen: () => void): void {
  useHotkeys(
    APP_SHORTCUTS.commandPalette.keys,
    () => onOpen(),
    { enableOnFormTags: true, enableOnContentEditable: true, preventDefault: true },
    [onOpen],
  );
}
