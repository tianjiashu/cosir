import type { ITerminalOptions, ITheme } from "@xterm/xterm";

/**
 * xterm 的字体与配色。
 *
 * xterm 在 canvas 上绘制，不能消费 CSS 变量，因此从 globals.css 的 `--app-font-mono` 与终端
 * token 读取实际值，保证 xterm 与周围容器使用同一套事实源。终端色 token 约定为十六进制 sRGB，
 * xterm 无法解析 oklch。
 *
 * 两种外观：
 * - `xtermAppearance()`：工具卡片里的终端岛（`terminal-surface`），两种主题下恒为深色。
 * - `xtermPaneTheme()`：底部终端工作台，跟随应用亮 / 暗主题；主题切换时由视图重新读取。
 */
function readTokens() {
  const tokens = getComputedStyle(document.documentElement);
  return (name: string) => tokens.getPropertyValue(name).trim();
}

export function xtermAppearance(): Pick<ITerminalOptions, "fontFamily" | "fontSize" | "lineHeight" | "theme"> {
  const read = readTokens();
  return {
    fontFamily: read("--app-font-mono"),
    fontSize: 12,
    lineHeight: 1.35,
    theme: {
      background: read("--terminal-inset"),
      foreground: read("--terminal-foreground"),
      cursor: read("--terminal-muted"),
      selectionBackground: read("--terminal-selection"),
    },
  };
}

/**
 * xterm 默认 ANSI 色板为深底设计，浅底上黄、白等色不可读；浅色主题换用 VS Code Light 的终端色板。
 * 16 色属于终端渲染细节，不进入全局 CSS token。
 */
const LIGHT_ANSI: ITheme = {
  black: "#000000",
  red: "#cd3131",
  green: "#107c10",
  yellow: "#949800",
  blue: "#0451a5",
  magenta: "#bc05bc",
  cyan: "#0598bc",
  white: "#555555",
  brightBlack: "#666666",
  brightRed: "#cd3131",
  brightGreen: "#14ce14",
  brightYellow: "#b5ba00",
  brightBlue: "#0451a5",
  brightMagenta: "#bc05bc",
  brightCyan: "#0598bc",
  brightWhite: "#a5a5a5",
};

export function xtermPaneTheme(resolvedTheme: "light" | "dark"): ITheme {
  const read = readTokens();
  return {
    ...(resolvedTheme === "light" ? LIGHT_ANSI : {}),
    background: read("--terminal-pane-background"),
    foreground: read("--terminal-pane-foreground"),
    cursor: read("--terminal-pane-cursor"),
    cursorAccent: read("--terminal-pane-background"),
    selectionBackground: read("--terminal-pane-selection"),
  };
}

export function xtermPaneFont(): Pick<ITerminalOptions, "fontFamily" | "fontSize" | "lineHeight"> {
  return { fontFamily: readTokens()("--app-font-mono"), fontSize: 12, lineHeight: 1.35 };
}
