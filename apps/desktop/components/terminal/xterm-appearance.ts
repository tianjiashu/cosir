import type { ITerminalOptions } from "@xterm/xterm";

/**
 * xterm 的字体与配色。
 *
 * xterm 在 canvas 上绘制，不能消费 CSS 变量，因此在创建实例时从 globals.css
 * 的 `--app-font-mono` 与 `--terminal-*` token 读取实际值，保证一次性命令卡片、
 * 交互终端与周围的 `terminal-surface` 容器使用同一套事实源。终端色 token
 * 约定为十六进制 sRGB，xterm 无法解析 oklch。
 */
export function xtermAppearance(): Pick<ITerminalOptions, "fontFamily" | "fontSize" | "lineHeight" | "theme"> {
  const tokens = getComputedStyle(document.documentElement);
  const read = (name: string) => tokens.getPropertyValue(name).trim();
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
