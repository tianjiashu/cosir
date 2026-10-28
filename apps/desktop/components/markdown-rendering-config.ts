import { code } from "@streamdown/code";
import { cjk } from "@streamdown/cjk";
import { math } from "@streamdown/math";
import { mermaid } from "@streamdown/mermaid";
import type { MermaidOptions } from "streamdown";
import "katex/dist/katex.min.css";

import { useThemeState, type ThemeState } from "@/lib/theme/theme-preference";

/**
 * 对话 Markdown 的稳定插件配置。
 *
 * 该配置只声明 Streamdown 的解析与展示能力，不负责消息状态、流式传输或持久化。
 * 模块级单例保证消息 token 更新时不会重新创建插件对象，具体渲染仍由
 * `StreamdownTextPrimitive` 按当前消息 part 的状态决定。
 */
export const markdownPlugins = {
  code,
  math,
  mermaid,
  cjk,
} as const;

type ResolvedTheme = ThemeState["resolvedTheme"];

/*
 * Mermaid 的 base 主题用 khroma 从这些色值推导其余颜色，只接受具体色值、不认
 * CSS 变量，因此这里按 globals.css 的中性色 token 手工换算成 hex；token 改动时同步。
 * 线条比 --border 深一档：--border 用作图形描边会在白底上几乎看不见。
 */
const MERMAID_PALETTE: Record<ResolvedTheme, Record<string, string>> = {
  light: {
    background: "#ffffff",
    primaryColor: "#f5f5f5",
    primaryTextColor: "#0a0a0a",
    primaryBorderColor: "#d4d4d4",
    secondaryColor: "#fafafa",
    tertiaryColor: "#ffffff",
    lineColor: "#737373",
    textColor: "#0a0a0a",
    noteBkgColor: "#fafafa",
    noteBorderColor: "#d4d4d4",
    noteTextColor: "#404040",
  },
  dark: {
    background: "#0a0a0a",
    primaryColor: "#262626",
    primaryTextColor: "#fafafa",
    primaryBorderColor: "#404040",
    secondaryColor: "#171717",
    tertiaryColor: "#0a0a0a",
    lineColor: "#a1a1a1",
    textColor: "#fafafa",
    noteBkgColor: "#171717",
    noteBorderColor: "#404040",
    noteTextColor: "#d4d4d4",
  },
};

const mermaidOptionsCache = new Map<ResolvedTheme, MermaidOptions>();

/*
 * 字体取应用的 --app-font-sans，而不是插件默认的 monospace：中文在等宽字体下
 * 字宽失衡。Mermaid 按此字体测量文字并写进 SVG，下载的 SVG/PNG 也会带上，
 * 所以必须是解析后的字体栈而非 var()。
 */
function buildMermaidOptions(theme: ResolvedTheme): MermaidOptions {
  const fontFamily = getComputedStyle(document.documentElement).getPropertyValue("--app-font-sans").trim();
  return {
    config: {
      securityLevel: "strict",
      theme: "base",
      darkMode: theme === "dark",
      fontFamily,
      themeVariables: { ...MERMAID_PALETTE[theme], fontFamily, fontSize: "14px" },
    },
  };
}

/**
 * 所有 Streamdown 实例共用的 Mermaid 选项，随应用明暗主题切换。
 *
 * 每个主题只构建一次并缓存：Streamdown 以 config 对象身份决定是否重渲染图表，
 * 同一主题下身份稳定，切换主题时身份变化会让已渲染的图表按新配色重绘。
 */
export function useMermaidOptions(): MermaidOptions {
  const { resolvedTheme } = useThemeState();
  let options = mermaidOptionsCache.get(resolvedTheme);
  if (!options) {
    options = buildMermaidOptions(resolvedTheme);
    mermaidOptionsCache.set(resolvedTheme, options);
  }
  return options;
}
