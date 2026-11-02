import type { ITerminalOptions, ITheme } from "@xterm/xterm";

/**
 * xterm 的字体与配色。
 *
 * xterm 的字体与配色只经构造选项以 JS 值传入，不能消费 CSS 变量，因此从 globals.css 的 `--app-font-mono` 与终端
 * token 读取实际值，保证 xterm 与周围容器使用同一套事实源。终端色 token 约定为十六进制 sRGB，
 * xterm 无法解析 oklch。
 *
 * 两处终端共用 `xtermPaneTheme`，都跟随应用亮 / 暗主题：底部终端工作台直接用它；对话里的命令输出视窗
 * （`TerminalOutputView`）用 `xtermCardTheme`——同一套前景与 ANSI 色板，背景透明，由容器的 `--surface`
 * 底面透出（`--surface` 是 color-mix，xterm 读不了，所以不把它交给 xterm）。
 */
function readTokens() {
  const tokens = getComputedStyle(document.documentElement);
  return (name: string) => tokens.getPropertyValue(name).trim();
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

/** 对话里命令输出视窗的配色：工作台的前景与色板，背景透明（容器须开 `allowTransparency`）。 */
export function xtermCardTheme(resolvedTheme: "light" | "dark"): ITheme {
  return { ...xtermPaneTheme(resolvedTheme), background: "#00000000" };
}

/** 卡片终端与工作台终端共用的字体。 */
export function xtermPaneFont(): Pick<ITerminalOptions, "fontFamily" | "fontSize" | "lineHeight"> {
  return { fontFamily: readTokens()("--app-font-mono"), fontSize: 12, lineHeight: 1.35 };
}
