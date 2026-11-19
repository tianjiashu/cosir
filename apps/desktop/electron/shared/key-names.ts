import type { HostPlatform } from "./host-contract";

/**
 * 按键词汇的唯一事实源：键位写法里每个部件（修饰键与主键）在三处的写法——界面展示、CodeMirror 键名、Electron
 * accelerator。快捷键登记表（`lib/keyboard/shortcuts.ts`）、编辑器键位（`components/code-editor/editor-keymap.ts`）
 * 与宿主菜单（`app-menu-keys.ts`）都只经这里转换；不认识的部件直接抛错，不原样放行——放行的键在编辑器或菜单里
 * 永远不会触发，又没有任何报错。
 *
 * 主键名是 `KeyboardEvent.code` 去掉 Key / Digit 前缀后的小写（按物理键位，与登记表的匹配一致）。
 * 修饰键：`mod` 是 macOS ⌘ / 其他平台 Ctrl，只在登记表的写法里出现，转换前由调用方换成 `meta` / `ctrl`
 * （Electron 例外，它有自己的 `CommandOrControl`）。
 */

type KeyName = {
  /** 展示文字；只给一份时各平台相同。 */
  label: string | { mac: string; other: string; linux?: string };
  codeMirror: string;
  electron: string;
};

const MODIFIERS: Record<string, KeyName> = {
  mod: { label: { mac: "⌘", other: "Ctrl" }, codeMirror: "Mod", electron: "CommandOrControl" },
  meta: { label: { mac: "⌘", other: "Win", linux: "Super" }, codeMirror: "Meta", electron: "Super" },
  ctrl: { label: { mac: "⌃", other: "Ctrl" }, codeMirror: "Ctrl", electron: "Control" },
  alt: { label: { mac: "⌥", other: "Alt" }, codeMirror: "Alt", electron: "Alt" },
  shift: { label: { mac: "⇧", other: "Shift" }, codeMirror: "Shift", electron: "Shift" },
};

const NAMED_KEYS: Record<string, KeyName> = {
  enter: { label: "↵", codeMirror: "Enter", electron: "Return" },
  escape: { label: "Esc", codeMirror: "Escape", electron: "Escape" },
  space: { label: "Space", codeMirror: "Space", electron: "Space" },
  tab: { label: "Tab", codeMirror: "Tab", electron: "Tab" },
  backspace: { label: { mac: "⌫", other: "Backspace" }, codeMirror: "Backspace", electron: "Backspace" },
  delete: { label: { mac: "⌦", other: "Delete" }, codeMirror: "Delete", electron: "Delete" },
  home: { label: "Home", codeMirror: "Home", electron: "Home" },
  end: { label: "End", codeMirror: "End", electron: "End" },
  pageup: { label: "PageUp", codeMirror: "PageUp", electron: "PageUp" },
  pagedown: { label: "PageDown", codeMirror: "PageDown", electron: "PageDown" },
  arrowup: { label: "↑", codeMirror: "ArrowUp", electron: "Up" },
  arrowdown: { label: "↓", codeMirror: "ArrowDown", electron: "Down" },
  arrowleft: { label: "←", codeMirror: "ArrowLeft", electron: "Left" },
  arrowright: { label: "→", codeMirror: "ArrowRight", electron: "Right" },
  backquote: { label: "`", codeMirror: "`", electron: "`" },
  minus: { label: "-", codeMirror: "-", electron: "-" },
  equal: { label: "=", codeMirror: "=", electron: "=" },
  bracketleft: { label: "[", codeMirror: "[", electron: "[" },
  bracketright: { label: "]", codeMirror: "]", electron: "]" },
  backslash: { label: "\\", codeMirror: "\\", electron: "\\" },
  semicolon: { label: ";", codeMirror: ";", electron: ";" },
  quote: { label: "'", codeMirror: "'", electron: "'" },
  comma: { label: ",", codeMirror: ",", electron: "," },
  period: { label: ".", codeMirror: ".", electron: "." },
  slash: { label: "/", codeMirror: "/", electron: "/" },
};

function keyName(part: string): KeyName {
  const known = MODIFIERS[part] ?? NAMED_KEYS[part];
  if (known) return known;
  if (/^[a-z0-9]$/.test(part)) return { label: part.toUpperCase(), codeMirror: part, electron: part.toUpperCase() };
  if (/^f([1-9]|1[0-9]|2[0-4])$/.test(part)) return { label: part.toUpperCase(), codeMirror: part.toUpperCase(), electron: part.toUpperCase() };
  throw new Error(`未登记的按键部件「${part}」：先加进 electron/shared/key-names.ts`);
}

export function isModifier(part: string): boolean {
  return part in MODIFIERS;
}

export function keyLabel(part: string, platform: HostPlatform): string {
  const { label } = keyName(part);
  if (typeof label === "string") return label;
  if (platform === "darwin") return label.mac;
  return platform === "linux" ? label.linux ?? label.other : label.other;
}

/** 具体键位（`ctrl+shift+bracketleft`）→ CodeMirror 键名（`Ctrl-Shift-[`）。 */
export function codeMirrorKey(keys: string): string {
  return keys.split("+").map((part) => keyName(part).codeMirror).join("-");
}

/** 键位写法（可含 `mod`）→ Electron accelerator（`mod+shift+z` → `CommandOrControl+Shift+Z`）。 */
export function electronAccelerator(keys: string): string {
  return keys.split("+").map((part) => keyName(part).electron).join("+");
}
